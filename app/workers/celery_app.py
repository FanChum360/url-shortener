"""
Celery Workers
==============
Async background tasks that run outside the request path.

Why async click logging?
  Without it, every redirect does:
    1. Cache check (~0.1ms)
    2. DB read if cache miss (~5ms)
    3. DB write (click count++) (~10ms) ← BLOCKING THE USER
    4. Redirect

  With async logging:
    1. Cache check (~0.1ms)
    2. Redis INCR for click buffer (~0.1ms)
    3. Redirect ← user is gone by now
    4. [background] Celery flushes to DB

  Result: Redirect P99 drops from ~20ms to ~2ms

Consistency tradeoff:
  - Click counts may lag by up to FLUSH_INTERVAL seconds
  - Acceptable for analytics (you don't need real-time counts)
  - NOT acceptable for billing (use synchronous writes there)
"""

import asyncio
from celery import Celery
from celery.schedules import crontab
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "url_shortener",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Beat schedule: periodic tasks
    beat_schedule={
        "flush-click-counts-every-30s": {
            "task": "app.workers.celery_app.flush_click_counts",
            "schedule": 30.0,  # every 30 seconds
        },
    },
)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,  # seconds between retries
    acks_late=True,  # Only ack after task completes (prevents data loss)
)
def log_click_event(self, short_code: str, ip_address: str, user_agent: str, referer: str):
    """
    Persist a single click event to the analytics DB.
    
    Called immediately on each redirect — logs granular analytics
    (IP, user-agent, referer) that can't be batched.
    """
    try:
        asyncio.run(_log_click_event_async(short_code, ip_address, user_agent, referer))
    except Exception as exc:
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


@celery_app.task
def flush_click_counts():
    """
    Periodic task: flush buffered click counts from Redis → PostgreSQL.
    
    Runs every 30 seconds. Reads all buffered counters and bulk-updates DB.
    This is more efficient than per-click DB writes at high throughput.
    """
    asyncio.run(_flush_click_counts_async())


async def _log_click_event_async(short_code, ip_address, user_agent, referer):
    from app.core.database import AsyncSessionLocal
    from app.models.url import ClickEvent

    async with AsyncSessionLocal() as session:
        event = ClickEvent(
            short_code=short_code,
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else None,
            referer=referer[:500] if referer else None,
        )
        session.add(event)
        await session.commit()


async def _flush_click_counts_async():
    """
    Bulk flush Redis click buffers to PostgreSQL.
    
    Uses SCAN to find all click buffer keys, reads counts,
    then does a single bulk UPDATE per short_code.
    """
    from app.core.cache import get_cache_service
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update, text
    from app.models.url import URL
    import redis.asyncio as aioredis

    settings_obj = get_settings()
    r = aioredis.from_url(settings_obj.redis_url, decode_responses=True)

    # Scan for all click buffer keys
    keys = []
    async for key in r.scan_iter("clicks:*:buffer"):
        keys.append(key)

    if not keys:
        return

    # Get and delete all counts atomically
    updates = {}
    pipe = r.pipeline()
    for key in keys:
        pipe.getdel(key)
    counts = await pipe.execute()

    for key, count in zip(keys, counts):
        if count:
            # Extract short_code from key pattern "clicks:{short_code}:buffer"
            short_code = key.split(":")[1]
            updates[short_code] = int(count)

    # Bulk update DB
    if updates:
        async with AsyncSessionLocal() as session:
            for short_code, count in updates.items():
                await session.execute(
                    update(URL)
                    .where(URL.short_code == short_code)
                    .values(click_count=URL.click_count + count)
                )
            await session.commit()

    await r.aclose()
