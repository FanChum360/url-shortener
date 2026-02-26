"""
Load Testing with Locust
=========================
Simulates realistic traffic patterns to benchmark the service.

Run with:
  locust -f tests/load_test.py --host=http://localhost:8000

Then open http://localhost:8089 for the Locust web UI.

Or headless:
  locust -f tests/load_test.py --host=http://localhost:8000 \
    --users 100 --spawn-rate 10 --run-time 60s --headless

Benchmark targets:
  - Redirect P99 latency: <50ms (with warm cache)
  - Shorten P99 latency: <200ms
  - Throughput: >5,000 RPS
  - Cache hit ratio: >80% (after warmup)
"""

import random
import string
from locust import HttpUser, task, between, events

# Pre-created short codes for redirect testing (populated during test)
SHORT_CODES = []


def random_url():
    path = "".join(random.choices(string.ascii_lowercase, k=10))
    return f"https://example.com/{path}"


class URLShortenerUser(HttpUser):
    """
    Simulates a real user:
      - 80% of traffic: redirects (read-heavy, should hit cache)
      - 15% of traffic: creating new short URLs
      - 5%  of traffic: checking stats
    """
    wait_time = between(0.1, 0.5)  # 0.1-0.5s think time between requests

    def on_start(self):
        """Warm up with some pre-created URLs."""
        for _ in range(5):
            self._create_url()

    def _create_url(self):
        """Create a URL and store the short code."""
        response = self.client.post(
            "/shorten",
            json={"url": random_url()},
            name="/shorten",
        )
        if response.status_code == 201:
            data = response.json()
            SHORT_CODES.append(data["short_code"])
            # Keep list bounded
            if len(SHORT_CODES) > 10_000:
                SHORT_CODES.pop(0)

    @task(8)  # 80% weight
    def redirect(self):
        """Simulate redirect — this is the hot path."""
        if not SHORT_CODES:
            self._create_url()
            return

        code = random.choice(SHORT_CODES)
        with self.client.get(
            f"/{code}",
            name="/{short_code} [redirect]",
            allow_redirects=False,  # Don't follow redirect — measure our server only
            catch_response=True,
        ) as response:
            if response.status_code in [302, 404, 410]:
                response.success()  # These are expected responses
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(1)  # 10% weight
    def shorten(self):
        """Create new short URLs."""
        self._create_url()

    @task(1)  # 10% weight
    def get_stats(self):
        """Fetch URL stats."""
        if not SHORT_CODES:
            return
        code = random.choice(SHORT_CODES)
        self.client.get(f"/stats/{code}", name="/stats/{short_code}")


class HighFrequencyRedirectUser(HttpUser):
    """
    Stress test: simulates a viral URL being hit very rapidly.
    All users hit the same 10 URLs → should be nearly 100% cache hit.
    """
    wait_time = between(0.01, 0.1)  # Very aggressive

    @task
    def redirect_popular(self):
        if not SHORT_CODES:
            return
        # Only hit the first 10 codes — simulates popular/viral URLs
        code = SHORT_CODES[0] if SHORT_CODES else "notfound"
        self.client.get(
            f"/{code}",
            name="/{short_code} [popular - cache test]",
            allow_redirects=False,
        )


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("\n🚀 Load test started")
    print("📊 Dashboard: http://localhost:8089")
    print("🎯 Target: <50ms P99 redirect, >5k RPS\n")
