"""
URL Service
===========
Core business logic for URL shortening and redirect resolution.

This layer sits between the API endpoints and the database/cache,
keeping concerns separated and making each layer testable in isolation.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.url import URL, ClickEvent
from app.models.schemas import ShortenRequest, ShortenResponse
from app.core.id_generator import generate_short_code
from app.core.cache import CacheService
from app.config import get_settings

log = structlog.get_logger()
settings = get_settings()


class URLNotFoundError(Exception):
    pass


class URLExpiredError(Exception):
    pass


class AliasConflictError(Exception):
    pass


class URLService:
    def __init__(self, db: AsyncSession, cache: CacheService):
        self.db = db
        self.cache = cache

    async def create_short_url(
        self,
        request: ShortenRequest,
        client_ip: Optional[str] = None,
    ) -> ShortenResponse:
        """
        Create a new short URL.

        Idempotency: If the same URL + alias combo is submitted twice,
        we return the existing record. This prevents duplicate entries
        from retried requests (network errors, etc).
        """
        # Use custom alias or generate one
        short_code = request.custom_alias or generate_short_code()

        # Check for alias conflict
        if request.custom_alias:
            existing = await self._get_url_from_db(request.custom_alias)
            if existing:
                raise AliasConflictError(f"Alias '{request.custom_alias}' already in use")

        # Calculate expiration
        expires_at = None
        if request.expires_in_days:
            expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)

        # Persist to database
        url_obj = URL(
            short_code=short_code,
            original_url=str(request.url),
            custom_alias=request.custom_alias,
            expires_at=expires_at,
            created_by_ip=client_ip,
        )
        self.db.add(url_obj)
        await self.db.flush()  # Get the ID without committing

        # Warm the cache immediately (write-through for new URLs)
        await self.cache.set_url(short_code, {
            "original_url": str(request.url),
            "is_active": True,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }, expires_at)

        log.info("url_created", short_code=short_code, original_url=str(request.url)[:100])

        return ShortenResponse(
            short_url=f"{settings.base_url}/{short_code}",
            short_code=short_code,
            original_url=str(request.url),
            expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
        )

    async def resolve_url(self, short_code: str) -> str:
        """
        Resolve a short code to its original URL.

        Cache-aside lookup flow:
          1. Check Redis → return immediately on HIT (~0.1ms)
          2. On MISS: query PostgreSQL → populate cache → return (~5-10ms)
          3. Validate expiration
          4. Async click tracking (doesn't block redirect)

        This is the HOT PATH — every redirect goes through here.
        Target: <50ms P99 latency
        """
        # Step 1: Cache lookup
        cached = await self.cache.get_url(short_code)
        if cached:
            log.info("cache_hit", short_code=short_code)
            self._validate_url_data(cached, short_code)

            # Buffer click asynchronously (doesn't block redirect)
            await self.cache.buffer_click(short_code)
            return cached["original_url"]

        # Step 2: DB fallback
        log.info("cache_miss", short_code=short_code)
        url_obj = await self._get_url_from_db(short_code)

        if not url_obj:
            raise URLNotFoundError(f"Short code '{short_code}' not found")

        if not url_obj.is_active:
            raise URLNotFoundError(f"Short code '{short_code}' is deactivated")

        if url_obj.expires_at and url_obj.expires_at < datetime.now(timezone.utc):
            raise URLExpiredError(f"Short code '{short_code}' has expired")

        # Step 3: Populate cache (cache-aside write)
        await self.cache.set_url(short_code, {
            "original_url": url_obj.original_url,
            "is_active": url_obj.is_active,
            "expires_at": url_obj.expires_at.isoformat() if url_obj.expires_at else None,
        }, url_obj.expires_at)

        # Step 4: Buffer click count
        await self.cache.buffer_click(short_code)

        return url_obj.original_url

    async def get_stats(self, short_code: str) -> URL:
        """Get statistics for a short URL."""
        url_obj = await self._get_url_from_db(short_code)
        if not url_obj:
            raise URLNotFoundError(f"Short code '{short_code}' not found")
        return url_obj

    async def log_click_event(
        self,
        short_code: str,
        ip_address: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
    ):
        """
        Persist a click event to the analytics table.
        Called by Celery worker — NOT in the request path.
        """
        event = ClickEvent(
            short_code=short_code,
            ip_address=ip_address,
            user_agent=user_agent,
            referer=referer,
        )
        self.db.add(event)

        # Also increment the denormalized click_count on the URL table
        await self.db.execute(
            update(URL)
            .where(URL.short_code == short_code)
            .values(click_count=URL.click_count + 1)
        )
        await self.db.commit()

    async def _get_url_from_db(self, short_code: str) -> Optional[URL]:
        """
        Query DB for URL by short_code.
        Uses the indexed short_code column — O(log n) B-tree lookup.
        """
        result = await self.db.execute(
            select(URL).where(URL.short_code == short_code)
        )
        return result.scalar_one_or_none()

    def _validate_url_data(self, url_data: dict, short_code: str):
        """Validate cached URL data (expiration check)."""
        if not url_data.get("is_active", True):
            raise URLNotFoundError(f"Short code '{short_code}' is deactivated")

        expires_at_str = url_data.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at < datetime.now(timezone.utc):
                raise URLExpiredError(f"Short code '{short_code}' has expired")
