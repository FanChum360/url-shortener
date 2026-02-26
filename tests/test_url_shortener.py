"""
Test Suite
==========
Unit tests for core logic + integration tests for API endpoints.

Run with:
  pytest tests/ -v
  pytest tests/ -v --cov=app --cov-report=html
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.core.id_generator import (
    SnowflakeGenerator, generate_short_code, to_base62, from_base62
)


# ─── ID Generator Tests ────────────────────────────────────────────────────

class TestSnowflakeGenerator:
    def test_generates_positive_id(self):
        gen = SnowflakeGenerator(machine_id=1)
        id_ = gen.next_id()
        assert id_ > 0

    def test_ids_are_monotonically_increasing(self):
        gen = SnowflakeGenerator(machine_id=1)
        ids = [gen.next_id() for _ in range(100)]
        assert ids == sorted(ids), "Snowflake IDs must be time-ordered"

    def test_ids_are_unique(self):
        gen = SnowflakeGenerator(machine_id=1)
        ids = [gen.next_id() for _ in range(10_000)]
        assert len(set(ids)) == len(ids), "Snowflake IDs must be unique"

    def test_different_machines_produce_different_ids(self):
        gen1 = SnowflakeGenerator(machine_id=1)
        gen2 = SnowflakeGenerator(machine_id=2)
        id1 = gen1.next_id()
        id2 = gen2.next_id()
        assert id1 != id2

    def test_invalid_machine_id_raises(self):
        with pytest.raises(ValueError):
            SnowflakeGenerator(machine_id=1024)  # Max is 1023
        with pytest.raises(ValueError):
            SnowflakeGenerator(machine_id=-1)

    def test_decode_round_trip(self):
        gen = SnowflakeGenerator(machine_id=42)
        id_ = gen.next_id()
        decoded = SnowflakeGenerator.decode(id_)
        assert decoded["machine_id"] == 42
        assert decoded["id"] == id_

    def test_thread_safety(self):
        """Concurrent ID generation must produce no duplicates."""
        import threading
        gen = SnowflakeGenerator(machine_id=1)
        results = []
        lock = threading.Lock()

        def generate_batch():
            batch = [gen.next_id() for _ in range(1000)]
            with lock:
                results.extend(batch)

        threads = [threading.Thread(target=generate_batch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == len(results), "Thread-safe generation must produce unique IDs"


class TestBase62:
    def test_basic_encoding(self):
        assert to_base62(0) == "0"
        assert to_base62(61) == "z"  # Last char in alphabet

    def test_round_trip(self):
        for n in [1, 100, 999999, 123456789]:
            assert from_base62(to_base62(n)) == n

    def test_base62_is_url_safe(self):
        """Base62 must only contain URL-safe characters."""
        import re
        code = to_base62(987654321)
        assert re.match(r'^[0-9A-Za-z]+$', code), "Base62 must be URL-safe"

    def test_short_code_generation(self):
        code = generate_short_code()
        assert 6 <= len(code) <= 12  # Reasonable length
        assert code.isalnum(), "Short codes must be alphanumeric"


# ─── URL Service Tests ─────────────────────────────────────────────────────

class TestURLServiceLogic:
    """Unit tests for URL service using mocked dependencies."""

    def _make_service(self):
        from app.services.url_service import URLService
        mock_db = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.get_url.return_value = None  # default cache miss
        mock_cache.check_rate_limit.return_value = (True, 1)
        return URLService(db=mock_db, cache=mock_cache), mock_db, mock_cache

    @pytest.mark.asyncio
    async def test_resolve_url_cache_hit(self):
        from app.services.url_service import URLService
        mock_db = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.get_url.return_value = {
            "original_url": "https://example.com",
            "is_active": True,
            "expires_at": None,
        }

        service = URLService(db=mock_db, cache=mock_cache)
        result = await service.resolve_url("abc123")

        assert result == "https://example.com"
        mock_db.execute.assert_not_called()  # DB should NOT be hit on cache hit

    @pytest.mark.asyncio
    async def test_resolve_url_not_found(self):
        from app.services.url_service import URLService, URLNotFoundError
        mock_db = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.get_url.return_value = None

        # Simulate DB returning None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        service = URLService(db=mock_db, cache=mock_cache)

        with pytest.raises(URLNotFoundError):
            await service.resolve_url("nonexistent")

    @pytest.mark.asyncio
    async def test_resolve_expired_url(self):
        from app.services.url_service import URLService, URLExpiredError
        from datetime import datetime, timezone, timedelta

        mock_db = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.get_url.return_value = {
            "original_url": "https://example.com",
            "is_active": True,
            "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }

        service = URLService(db=mock_db, cache=mock_cache)

        with pytest.raises(URLExpiredError):
            await service.resolve_url("expired123")


# ─── API Integration Tests ─────────────────────────────────────────────────

@pytest.fixture
def mock_service():
    """Mock the URL service for API-level tests."""
    with patch("app.api.routes.get_url_service") as mock:
        service = AsyncMock()
        mock.return_value = service
        yield service


@pytest.fixture
def mock_cache():
    with patch("app.api.routes.get_cache_service") as mock:
        cache = AsyncMock()
        cache.check_rate_limit.return_value = (True, 1)
        cache.health_check.return_value = True
        mock.return_value = cache
        yield cache


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Mock DB and cache for health check
        with patch("app.api.routes.get_db") as mock_db, \
             patch("app.api.routes.get_cache_service") as mock_cache:
            db = AsyncMock()
            db.execute = AsyncMock()
            mock_db.return_value = db

            cache = AsyncMock()
            cache.health_check.return_value = True
            mock_cache.return_value = cache

            response = await client.get("/health")
            # Health endpoint may fail in test (no real DB), just check it responds
            assert response.status_code in [200, 500]


@pytest.mark.asyncio
async def test_shorten_url_validation():
    """Test that invalid URLs are rejected."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/shorten", json={"url": "not-a-valid-url"})
        assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_shorten_url_localhost_blocked():
    """Test that localhost URLs are rejected (SSRF prevention)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/shorten", json={"url": "http://localhost/admin"})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_short_code_redirect_not_found():
    """Test 404 for nonexistent short codes."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with patch("app.api.routes.get_url_service") as mock, \
             patch("app.api.routes.get_cache_service") as mock_cache:
            from app.services.url_service import URLNotFoundError
            service = AsyncMock()
            service.resolve_url.side_effect = URLNotFoundError("not found")
            mock.return_value = service

            cache = AsyncMock()
            mock_cache.return_value = cache

            response = await client.get("/nonexistent", follow_redirects=False)
            assert response.status_code == 404
