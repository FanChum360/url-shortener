-- URL Shortener Database Initialization
-- =======================================
-- Indexes are the most important performance optimization here.
-- Every redirect hits the short_code lookup — it MUST be fast.

-- The tables are created by SQLAlchemy on startup,
-- but we add additional performance config here.

-- Explain the index strategy:
-- B-tree index on short_code:
--   - INSERT: O(log n) to find insertion point
--   - SELECT WHERE short_code = 'abc': O(log n) — scans tree, not full table
--   - At 10M rows: ~23 comparisons vs 10,000,000 full scan
--   - B-tree chosen over Hash index because:
--       1. PostgreSQL's hash indexes don't support range queries
--       2. B-tree supports: =, <, >, BETWEEN, LIKE 'prefix%'
--       3. B-tree can be used for ORDER BY (hash cannot)

-- Analyze query performance (run after load test):
-- EXPLAIN ANALYZE SELECT * FROM urls WHERE short_code = 'abc123';
-- 
-- Expected output:
--   Index Scan using ix_urls_short_code on urls
--     Index Cond: ((short_code)::text = 'abc123'::text)
--     Planning Time: 0.1 ms
--     Execution Time: 0.05 ms  ← sub-millisecond with warm cache

-- Partial index for active URLs only (reduces index size):
-- CREATE INDEX CONCURRENTLY ix_urls_active_short_code
--   ON urls (short_code)
--   WHERE is_active = true;

-- Set statement timeout to prevent slow query abuse
ALTER DATABASE urlshortener SET statement_timeout = '5s';

-- Enable pg_stat_statements for query analysis
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
