"""
Database Engine & Session
=========================
Async SQLAlchemy setup with connection pooling.

Connection pooling explained:
  - pool_size: persistent connections kept open
  - max_overflow: temporary extra connections under load
  - pool_pre_ping: validates connections before use (handles DB restarts)
  - pool_recycle: closes connections older than N seconds (prevents stale connections)
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.config import get_settings
from app.models.url import Base

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    # Connection pool configuration
    pool_size=20,           # Baseline pool — keeps 20 connections warm
    max_overflow=10,        # Allows burst to 30 under heavy load
    pool_pre_ping=True,     # Test connections before use (handles DB failover)
    pool_recycle=3600,      # Recycle connections every hour (prevents MySQL/PG timeouts)
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Don't expire objects after commit (avoids extra queries)
)


async def create_tables():
    """Create all tables. In production, use Alembic migrations instead."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency: yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
