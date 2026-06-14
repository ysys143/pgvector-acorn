-- tier2_scan_stats.sql: per-scan telemetry counters (borrow-list Priority 5).
--
-- pg_acorn_scan_stats() exposes backend-local cumulative Tier-2 (AM) scan
-- counters; pg_acorn_scan_stats_reset() zeroes them.  This test asserts the
-- INVARIANTS that define correct wiring (not exact graph-work counts, which are
-- deterministic but brittle): reset zeroes everything; a cache-OFF scan does
-- work with no cache hits; a warm cache-ON scan is served from the cache and
-- re-ranks; the scan counter is exact and accumulates across scans.
--
-- Determinism: setseed for data, pg_acorn.build_seed + serial build for the
-- graph; single-backend (no parallel workers) since the counters are
-- backend-local; assertions output booleans only.

\set ON_ERROR_STOP on
\set q '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'

CREATE SCHEMA test_tier2_stats;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_tier2_stats, public;

-- 600 rows, 10 buckets -> bucket < 3 = ~30% selectivity (180 rows).
SELECT setseed(0.42);
CREATE TABLE items (
    id        serial PRIMARY KEY,
    bucket    int,
    embedding vector(8)
);

INSERT INTO items (bucket, embedding) SELECT
    (i % 10),
    ('[' || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ']')::vector
FROM generate_series(1, 600) i;

-- Deterministic NON-inline index (so the code cache can serve it).
SET pg_acorn.build_seed = 42;
SET max_parallel_maintenance_workers = 0;
CREATE INDEX items_idx ON items
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2);
RESET pg_acorn.build_seed;

-- Single-backend index scans (counters are backend-local).
SET enable_seqscan = off;
SET max_parallel_workers_per_gather = 0;
SET pg_acorn.ef_search = 200;

-- 1. reset zeroes every counter.
SELECT pg_acorn_scan_stats_reset();
SELECT scans = 0 AND expansions = 0 AND cc_hits = 0
   AND loads = 0 AND reranks = 0 AND emits = 0 AS reset_all_zero
FROM pg_acorn_scan_stats();

-- 2. one cache-OFF filtered scan: exactly one scan, graph work done, element
--    pages loaded, NO code-cache hits, at least k tuples emitted.
SET pg_acorn.scan_code_cache = off;
SELECT pg_acorn_scan_stats_reset();
SELECT count(*) FROM (
    SELECT id FROM items WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10) t;
SELECT scans = 1      AS one_scan,
       expansions > 0 AS did_expand,
       loads > 0      AS did_load,
       cc_hits = 0    AS no_cache_hits_when_off,
       emits >= 10    AS emitted_at_least_k
FROM pg_acorn_scan_stats();

-- 3. warm cache-ON scan: discovery is served from the shared cache (cc_hits>0)
--    and approx ordering forces exact re-ranks (reranks>0).  A warm-up scan
--    first triggers the cold load so the measured scan finds a READY slot.
SET pg_acorn.scan_code_cache = on;
SELECT count(*) FROM (
    SELECT id FROM items WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10) t;   -- cold load
SELECT pg_acorn_scan_stats_reset();
SELECT count(*) FROM (
    SELECT id FROM items WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10) t;   -- measured (warm)
SELECT scans = 1    AS one_scan,
       cc_hits > 0  AS cache_served,
       reranks > 0  AS reranked_when_approx,
       emits >= 10  AS emitted_at_least_k
FROM pg_acorn_scan_stats();

-- 4. counters accumulate across scans (no reset between the two).
SELECT pg_acorn_scan_stats_reset();
SELECT count(*) FROM (
    SELECT id FROM items WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10) t;
SELECT count(*) FROM (
    SELECT id FROM items WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10) t;
SELECT scans = 2 AS two_scans_accumulated FROM pg_acorn_scan_stats();

RESET pg_acorn.scan_code_cache;
RESET pg_acorn.ef_search;
RESET enable_seqscan;

DROP SCHEMA test_tier2_stats CASCADE;
