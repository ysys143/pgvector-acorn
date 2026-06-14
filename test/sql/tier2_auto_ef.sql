-- tier2_auto_ef.sql: selectivity-aware auto-ef (borrow-list Priority 3).
--
-- pg_acorn.target_recall > 0 makes a Tier-2 scan derive its expansion budget
-- (ef) from the estimated filter selectivity, read from a build-time histogram
-- in the meta page, instead of the manual pg_acorn.ef_search.  This test asserts
-- the WIRING + HEURISTIC invariants deterministically, using the P5 scan-stats
-- `expansions` counter as the observable for the effective ef (buffered_emission
-- defaults on, so expansions tracks the ef budget).  Recall IMPROVEMENT from the
-- larger budget follows by HNSW theory and is validated in bench/ (not asserted
-- here, to keep the golden file fixture-insensitive).
--
-- Determinism: setseed for data, build_seed + serial build for the graph (and an
-- exact histogram, since 2000 rows < the sample cap); single backend (counters
-- are backend-local); assertions output booleans only.

\set ON_ERROR_STOP on
\set q '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'

CREATE SCHEMA test_tier2_autoef;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_tier2_autoef, public;

-- 2000 rows, 20 buckets: bucket < k => k/20 selectivity (bucket<2=10%, <10=50%).
SELECT setseed(0.42);
CREATE TABLE items (
    id        serial PRIMARY KEY,
    bucket    int,
    embedding vector(8)
);

INSERT INTO items (bucket, embedding) SELECT
    (i % 20),
    ('[' || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ']')::vector
FROM generate_series(1, 2000) i;

SET pg_acorn.build_seed = 42;
SET max_parallel_maintenance_workers = 0;
CREATE INDEX items_idx ON items
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2);
RESET pg_acorn.build_seed;

SET enable_seqscan = off;
SET max_parallel_workers_per_gather = 0;
SET pg_acorn.buffered_emission = on;	-- expansions tracks the ef budget

-- Helper: run one filtered top-10 scan, return this scan's expansions/emits.
CREATE FUNCTION scan_stats(sel int) RETURNS TABLE(expansions bigint, emits bigint)
AS $$
BEGIN
    PERFORM pg_acorn_scan_stats_reset();
    PERFORM id FROM items WHERE bucket < sel
        ORDER BY embedding <-> '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'::vector LIMIT 10;
    RETURN QUERY SELECT s.expansions, s.emits FROM pg_acorn_scan_stats() s;
END;
$$ LANGUAGE plpgsql;

-- 1. WIRING: a tiny manual ef (target_recall=0) expands far less than auto-ef at
--    the same selectivity (sel 50%) — proving the histogram is read, selectivity
--    estimated, and the budget overridden.  Also: target_recall=0 is NOT
--    auto-overridden (manual ef respected => small expansions).
SET pg_acorn.target_recall = 0;
SET pg_acorn.ef_search = 10;
CREATE TABLE e_manual AS SELECT * FROM scan_stats(10);

SET pg_acorn.target_recall = 0.95;
CREATE TABLE e_auto AS SELECT * FROM scan_stats(10);

SELECT (SELECT expansions FROM e_auto) > (SELECT expansions FROM e_manual)
                                           AS autoef_raises_ef,
       (SELECT expansions FROM e_manual) <= 20 AS manual_ef_respected,
       (SELECT emits FROM e_auto) >= 10        AS autoef_returns_k;

-- 2. MONOTONE in selectivity: higher selectivity -> larger derived ef -> more
--    expansions (target_recall fixed).
CREATE TABLE e_sel10 AS SELECT * FROM scan_stats(2);   -- 10%
CREATE TABLE e_sel50 AS SELECT * FROM scan_stats(10);  -- 50%
SELECT (SELECT expansions FROM e_sel50) >= (SELECT expansions FROM e_sel10)
                                           AS sel_monotone;

-- 3. MONOTONE in target_recall: higher recall target -> larger ef (fixed sel).
SET pg_acorn.target_recall = 0.90;
CREATE TABLE e_r90 AS SELECT * FROM scan_stats(2);
SET pg_acorn.target_recall = 0.99;
CREATE TABLE e_r99 AS SELECT * FROM scan_stats(2);
SELECT (SELECT expansions FROM e_r99) >= (SELECT expansions FROM e_r90)
                                           AS recall_monotone;

-- 4. CORRECTNESS preserved under auto-ef: k results, all passing the filter.
SET pg_acorn.target_recall = 0.95;
SELECT bool_and(bucket < 10) AS filter_correct,
       count(*) = 10         AS returns_k
FROM (SELECT bucket FROM items WHERE bucket < 10
      ORDER BY embedding <-> :'q'::vector LIMIT 10) r;

RESET pg_acorn.target_recall;
RESET pg_acorn.ef_search;
RESET pg_acorn.buffered_emission;
RESET enable_seqscan;

DROP SCHEMA test_tier2_autoef CASCADE;
