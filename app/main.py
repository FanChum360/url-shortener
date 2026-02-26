"""
URL Shortener — FastAPI Application
=====================================
Production-grade setup with:
  - Structured logging (structlog)
  - Prometheus metrics
  - Request ID tracing
  - CORS middleware
  - Lifespan management (DB init, connection warmup)
"""

import time
import uuid
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.routes import router
from app.core.database import create_tables
from app.config import get_settings

settings = get_settings()

# ─── Structured Logging Setup ────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),  # JSON logs for log aggregators
    ],
)

log = structlog.get_logger()


# ─── Application Lifespan ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown logic.
    FastAPI's modern replacement for @app.on_event("startup").
    """
    log.info("startup", app=settings.app_name, version=settings.app_version)
    await create_tables()
    log.info("database_ready")
    yield
    log.info("shutdown")


# ─── App Factory ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="URL Shortener API",
    description="""
## Production-Grade Distributed URL Shortener

### Architecture Highlights
- **Snowflake IDs**: Distributed unique ID generation (no coordination needed)
- **Base62 encoding**: URL-safe short codes
- **Redis caching**: Cache-aside pattern, <50ms redirect target
- **Async click logging**: Celery workers for non-blocking analytics
- **Rate limiting**: Sliding window via Redis atomic ops
- **Connection pooling**: SQLAlchemy async pool for high throughput

### Key Design Decisions
- `302` redirects (not 301) to enable analytics on every request
- Click counts buffered in Redis, flushed to DB every 30s
- Short codes from last 8 chars of Base62(Snowflake ID)
    """,
    version=settings.app_version,
    lifespan=lifespan,
)

# ─── Middleware ───────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """
    Adds request tracing and latency logging to every request.
    
    X-Request-ID: allows tracing a single request across all services
    Latency logging: critical for identifying P99 regressions
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    # Bind request context to structured logger
    with structlog.contextvars.bound_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    ):
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        log.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
        return response


# ─── Prometheus Metrics ───────────────────────────────────────────────────────
# Exposes /metrics endpoint for Prometheus scraping
# Tracks: request count, latency histograms, error rates
Instrumentator().instrument(app).expose(app)


# ─── Routes ───────────────────────────────────────────────────────────────────
app.include_router(router)
