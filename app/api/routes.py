"""
API Routes
==========
FastAPI endpoints for the URL shortener service.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.database import get_db
from app.core.cache import get_cache_service, CacheService
from app.models.schemas import ShortenRequest, ShortenResponse, URLStatsResponse, HealthResponse
from app.services.url_service import URLService, URLNotFoundError, URLExpiredError, AliasConflictError
from app.config import get_settings

log = structlog.get_logger()
settings = get_settings()

router = APIRouter()


def get_url_service(
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> URLService:
    return URLService(db=db, cache=cache)


def get_client_ip(request: Request) -> str:
    """Extract real client IP, accounting for reverse proxies."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── URL Shortening ──────────────────────────────────────────────────────────

@router.post(
    "/shorten",
    response_model=ShortenResponse,
    status_code=201,
    summary="Create a short URL",
    description="""
    Shorten a long URL. Optionally set a custom alias and expiration.
    
    **Rate limited**: 100 requests per IP per minute.
    """,
)
async def shorten_url(
    request: Request,
    body: ShortenRequest,
    service: URLService = Depends(get_url_service),
    cache: CacheService = Depends(get_cache_service),
):
    client_ip = get_client_ip(request)

    # Rate limiting check
    allowed, count = await cache.check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "message": f"Max {settings.rate_limit_requests} requests per {settings.rate_limit_window_seconds}s",
                "current_count": count,
            },
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )

    try:
        result = await service.create_short_url(body, client_ip=client_ip)
        return result
    except AliasConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        log.error("shorten_error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Redirect ────────────────────────────────────────────────────────────────

@router.get(
    "/{short_code}",
    summary="Redirect to original URL",
    description="""
    Resolves a short code and redirects to the original URL.
    
    Uses **302 (temporary redirect)** intentionally:
    - Allows analytics tracking on every request
    - Browser doesn't cache 302s (unlike 301)
    - 301 would bypass our server after first visit — no analytics!
    
    Cache-aside lookup: Redis first, then PostgreSQL on miss.
    """,
    responses={
        302: {"description": "Redirect to original URL"},
        404: {"description": "Short code not found"},
        410: {"description": "URL has expired"},
    },
)
async def redirect_url(
    short_code: str,
    request: Request,
    service: URLService = Depends(get_url_service),
):
    try:
        original_url = await service.resolve_url(short_code)

        # Async click analytics — fire and forget
        # Using FastAPI background tasks (simpler than Celery for this)
        _enqueue_click_event(
            short_code=short_code,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            referer=request.headers.get("Referer", ""),
        )

        return RedirectResponse(url=original_url, status_code=302)

    except URLNotFoundError:
        raise HTTPException(status_code=404, detail="Short URL not found")
    except URLExpiredError:
        raise HTTPException(status_code=410, detail="This short URL has expired")


def _enqueue_click_event(short_code, ip_address, user_agent, referer):
    """
    Enqueue click analytics without blocking the redirect.
    Uses Celery if available, falls back to fire-and-forget.
    """
    try:
        from app.workers.celery_app import log_click_event
        log_click_event.delay(short_code, ip_address, user_agent, referer)
    except Exception:
        # If Celery is unavailable, log and continue (redirect still works)
        log.warning("celery_unavailable", short_code=short_code)


# ─── Stats & Analytics ───────────────────────────────────────────────────────

@router.get(
    "/stats/{short_code}",
    response_model=URLStatsResponse,
    summary="Get URL statistics",
)
async def get_url_stats(
    short_code: str,
    service: URLService = Depends(get_url_service),
):
    try:
        url_obj = await service.get_stats(short_code)
        return URLStatsResponse(
            short_code=url_obj.short_code,
            original_url=url_obj.original_url,
            click_count=url_obj.click_count,
            created_at=url_obj.created_at,
            expires_at=url_obj.expires_at,
            is_active=url_obj.is_active,
        )
    except URLNotFoundError:
        raise HTTPException(status_code=404, detail="Short URL not found")


# ─── Health Check ─────────────────────────────────────────────────────────────

@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Service health check",
)
async def health_check(
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
):
    """
    Liveness + readiness probe.
    Returns status of all dependencies (DB, Redis).
    Used by load balancers to route traffic away from unhealthy instances.
    """
    db_status = "healthy"
    cache_status = "healthy"

    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
    except Exception:
        db_status = "unhealthy"

    cache_healthy = await cache.health_check()
    if not cache_healthy:
        cache_status = "unhealthy"

    overall = "healthy" if db_status == "healthy" and cache_status == "healthy" else "degraded"

    return HealthResponse(
        status=overall,
        database=db_status,
        cache=cache_status,
        version=settings.app_version,
    )
