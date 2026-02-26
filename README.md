# 🔗 Distributed URL Shortener

A production-grade URL shortening service built with FastAPI, Redis, and PostgreSQL. This project implements core distributed systems concepts including Snowflake ID generation, cache-aside pattern, async click analytics, and rate limiting — making it ideal for system design interview preparation.

## Project Overview

This system allows users to shorten long URLs, set custom aliases and expiration dates, and track click analytics. It is designed for high read throughput with sub-50ms redirect latency, horizontal scalability, and fault tolerance. The full stack runs locally via Docker with a single command.

## Key Features

- **Snowflake ID Generation** for distributed unique ID creation without coordination
- **Base62 Encoding** for short, URL-safe codes
- **Redis Cache-Aside** for fast redirects with 80%+ cache hit ratio
- **Async Click Analytics** via Celery workers to avoid blocking redirects
- **Rate Limiting** per IP using Redis atomic operations
- **Custom Aliases** and expiration support
- **Prometheus + Grafana** metrics dashboard included
- **Load Testing** with Locust to benchmark throughput and latency

## Tools & Libraries

- **Python 3.11+**
- **FastAPI** – async REST API framework
- **PostgreSQL 15** – persistent URL storage with B-tree indexed lookups
- **Redis 7** – caching layer and rate limit counters
- **SQLAlchemy (async)** – ORM with connection pooling
- **Celery** – background worker for async click processing
- **Prometheus + Grafana** – observability and metrics
- **Locust** – load testing and benchmarking
- **Docker + Docker Compose** – containerized deployment

## Methodology

1. **ID Generation** – Snowflake algorithm produces a 64-bit time-ordered ID (timestamp + machine ID + sequence). Converted to Base62 for a short, URL-safe code.
2. **URL Creation** – Validated URL stored in PostgreSQL. Cache warmed immediately on write.
3. **Redirect Flow** – Redis checked first (~0.1ms on hit). On miss, PostgreSQL queried via indexed `short_code` lookup, result written to cache.
4. **Click Tracking** – Click count buffered in Redis via atomic `INCR`. Celery worker flushes to DB every 30 seconds, keeping redirects fast.
5. **Rate Limiting** – Per-IP request counter stored in Redis with automatic TTL expiry.

## Example Workflow

1. Start the service: `docker compose up --build`
2. Open `http://localhost:8000/docs` to explore the API
3. Shorten a URL via the `/shorten` endpoint
4. Visit the short URL in a browser to trigger a redirect
5. Check click stats at `/stats/{short_code}`
6. View metrics at `http://localhost:3000` (Grafana)

## Example API Usage

**Shorten a URL**

Request:
```bash
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/very-long-path", "expires_in_days": 30}'
```

Response:
```json
{
  "short_url": "http://localhost:8000/aB3cD4eF",
  "short_code": "aB3cD4eF",
  "original_url": "https://example.com/very-long-path",
  "expires_at": "2024-07-01T00:00:00Z",
  "created_at": "2024-06-01T00:00:00Z"
}
```

**Redirect**
```bash
curl -L http://localhost:8000/aB3cD4eF
```

**Get Stats**
```bash
curl http://localhost:8000/stats/aB3cD4eF
```

## How to Run

1. Install **Docker Desktop**: [docker.com/products/docker-desktop](https://docker.com/products/docker-desktop)
2. Clone the repository:
```bash
git clone https://github.com/you/url-shortener.git
cd url-shortener
```
3. Start all services:
```bash
docker compose up --build
```
4. Open `http://localhost:8000/docs` to use the API
5. (Optional) Run tests:
```bash
pip install -r requirements.txt
pytest tests/test_url_shortener.py -v
```
6. (Optional) Run load test:
```bash
locust -f tests/load_test.py --host=http://localhost:8000
```

## Outcomes

- Shorten and resolve URLs with sub-50ms redirect latency
- Handle high read throughput with Redis cache absorbing 80%+ of traffic
- Track click analytics asynchronously without impacting redirect speed
- Enforce per-IP rate limits across horizontally scaled instances
- Monitor live traffic with Prometheus metrics and Grafana dashboards
- Benchmark performance with Locust load tests

## License

This project is open-source and available under the MIT License.
