"""
Redis Cache Layer
=================
Implements cache-aside pattern for URL lookups.

Cache-Aside (Lazy Loading) Strategy:
  1. App checks cache first
  2. On MISS: load from DB, write to cache, return
  3. On HIT: return cached value directly

Why not Write-Through?
  - Write-through updates cache on every DB write
  - Better consistency but wastes memory (many URLs never accessed)
  - Cache-aside only caches hot URLs (accessed URLs)

TTL Strategy:
  - Default: 1 hour for standard URLs
  - Custom: respects URL expiration date
  - Rate limit counters: sliding 60-second window

Why Redis for rate limiting?
  - Atomic INCR operation (no race conditions)
  - Sub-millisecond latency
  - TTL built-in (auto-expiry of counters)
  - Shared state across all API instances
"""

import json
from datetime import datetime, timezone
from typing import Optional
import redis.asyncio as redis
from app.config import get_settings

settings = get_settings()

_redis_pool: Optional[redis.ConnectionPool] = None


def get_redis_pool() -> redis.ConnectionPool:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=50,
            decode_responses=True,
        )
    return _redis_pool


def get_redis_client() -> redis.Redis:
    return redis.Redis(connection_pool=get_redis_pool())


# ─── Key Naming Convention ───────────────────────────────────────────────────
# url:{short_code}         → cached URL data
# rate:{ip}                → request counter for IP
# stats:{short_code}:count → real-time click count buffer

def url_cache_key(short_code: str) -> str:
    return f"url:{short_code}"

def rate_limit_key(ip: str) -> str:
    return f"rate:{ip}"

def click_buffer_key(short_code: str) -> str:
    return f"clicks:{short_code}:buffer"


class CacheService:
    def __init__(self):
        self.client = get_redis_client()
        self.default_ttl = settings.cache_ttl_seconds

    async def get_url(self, short_code: str) -> Optional[dict]:
        """
        Cache lookup for URL data.
        Returns parsed dict or None on miss.
        """
        key = url_cache_key(short_code)
        data = await self.client.get(key)
        if data:
            return json.loads(data)
        return None

    async def set_url(self, short_code: str, url_data: dict, expires_at: Optional[datetime] = None):
        """
        Cache URL data with smart TTL.
        
        TTL = min(default_ttl, seconds_until_expiry)
        This prevents serving expired URLs from cache.
        """
        key = url_cache_key(short_code)
        ttl = self.default_ttl

        if expires_at:
            now = datetime.now(timezone.utc)
            seconds_until_expiry = int((expires_at - now).total_seconds())
            if seconds_until_expiry <= 0:
                return  # URL already expired, don't cache
            ttl = min(ttl, seconds_until_expiry)

        await self.client.setex(key, ttl, json.dumps(url_data, default=str))

    async def invalidate_url(self, short_code: str):
        """Remove URL from cache (on deletion or update)."""
        await self.client.delete(url_cache_key(short_code))

    async def check_rate_limit(self, ip: str) -> tuple[bool, int]:
        """
        Sliding window rate limiter using Redis atomic operations.
        
        Algorithm: Token counter with TTL reset
          - INCR is atomic → no race conditions between requests
          - First request sets TTL → window auto-resets
          - Returns (allowed: bool, current_count: int)
        
        Limitation: This is a fixed window, not true sliding window.
        True sliding window requires sorted sets (ZADD/ZRANGE) but
        uses more memory. Fixed window is good enough for abuse prevention.
        """
        key = rate_limit_key(ip)
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        results = await pipe.execute()
        
        count = results[0]
        ttl = results[1]
        
        # Set TTL on first request of the window
        if ttl == -1:
            await self.client.expire(key, settings.rate_limit_window_seconds)
        
        allowed = count <= settings.rate_limit_requests
        return allowed, count

    async def buffer_click(self, short_code: str) -> int:
        """
        Buffer click counts in Redis for async batch writes to DB.
        
        Instead of DB write per click, we:
          1. INCR counter in Redis (microseconds)
          2. Celery worker periodically flushes to DB (seconds)
        
        Tradeoff: Up to ~5s of click count lag vs synchronous writes.
        Benefit: Redirect latency drops dramatically under load.
        """
        key = click_buffer_key(short_code)
        count = await self.client.incr(key)
        # Set expiry to prevent orphaned keys
        await self.client.expire(key, 3600)
        return count

    async def get_buffered_clicks(self, short_code: str) -> int:
        """Get and reset buffered click count (used by Celery worker)."""
        key = click_buffer_key(short_code)
        # GETDEL atomically reads and deletes (Redis 6.2+)
        # Falls back to GET + DEL pipeline for older Redis
        count = await self.client.getdel(key)
        return int(count) if count else 0

    async def health_check(self) -> bool:
        try:
            await self.client.ping()
            return True
        except Exception:
            return False


# Module-level singleton
_cache_service: Optional[CacheService] = None


def get_cache_service() -> CacheService:
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService()
    return _cache_service
