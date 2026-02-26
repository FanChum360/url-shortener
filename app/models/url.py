"""
Database Models
===============
SQLAlchemy async models for the URL shortener service.

Design decisions:
  - BigInteger primary key (future-proof for scale)
  - short_code indexed — this is the hot path for every redirect
  - click_count on main table (good enough for MVP; move to separate table at scale)
  - expires_at nullable — URLs can live forever or have TTL
"""

from datetime import datetime
from sqlalchemy import (
    BigInteger, String, Text, DateTime, Integer,
    Boolean, Index, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class URL(Base):
    """
    Core URL mapping table.

    Read-heavy: every redirect hits this table (or Redis cache).
    Write-light: only on create + async click increments.

    Index strategy:
      - short_code: B-tree index, O(log n) lookups
        B-tree chosen over hash because:
          1. Supports range scans (BETWEEN, <, >)
          2. Works with LIKE 'prefix%'
          3. PostgreSQL planner can use it for ORDER BY
    """
    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    short_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    custom_alias: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max length

    __table_args__ = (
        # Composite index for active URL lookups — covers most queries
        Index("ix_urls_short_code_active", "short_code", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<URL id={self.id} short_code={self.short_code!r}>"


class ClickEvent(Base):
    """
    Analytics table for click tracking.

    Written asynchronously via Celery to avoid slowing down redirects.
    
    At scale: partition by created_at (monthly), use columnar storage,
    or stream to data warehouse (BigQuery/Redshift).
    """
    __tablename__ = "click_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    short_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Compound index for analytics queries: "clicks for code X in date range"
        Index("ix_click_events_code_time", "short_code", "clicked_at"),
    )
