from datetime import datetime
from typing import Optional
from pydantic import BaseModel, HttpUrl, field_validator, model_validator
import validators


class ShortenRequest(BaseModel):
    url: str
    custom_alias: Optional[str] = None
    expires_in_days: Optional[int] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not validators.url(v):
            raise ValueError("Invalid URL format")
        if len(v) > 2048:
            raise ValueError("URL exceeds maximum length of 2048 characters")
        # Block localhost/private IPs in production (SSRF prevention)
        blocked = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
        for blocked_host in blocked:
            if blocked_host in v.lower():
                raise ValueError("URL points to a private/local address")
        return v

    @field_validator("custom_alias")
    @classmethod
    def validate_alias(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.isalnum() and not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("Custom alias must be alphanumeric (hyphens and underscores allowed)")
        if len(v) < 3 or len(v) > 32:
            raise ValueError("Custom alias must be between 3 and 32 characters")
        return v

    @field_validator("expires_in_days")
    @classmethod
    def validate_expiry(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 1 or v > 3650):
            raise ValueError("expiry must be between 1 and 3650 days")
        return v


class ShortenResponse(BaseModel):
    short_url: str
    short_code: str
    original_url: str
    expires_at: Optional[datetime] = None
    created_at: datetime


class URLStatsResponse(BaseModel):
    short_code: str
    original_url: str
    click_count: int
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_active: bool


class HealthResponse(BaseModel):
    status: str
    database: str
    cache: str
    version: str
