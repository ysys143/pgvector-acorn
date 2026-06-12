-- tier2_build_mwm.sql: maintenance_work_mem enforcement during build.
--
-- With a tiny maintenance_work_mem the in-memory graph must stop growing,
-- flush what was built, and finish the build through the per-element
-- on-disk insert path (pgvector's two-phase build pattern), emitting ONE
-- WARNING carrying both numbers (kB used vs kB budget).  The index built
-- this way must answer REAL-recall queries (exact seqscan truth table —
-- never tautological CTEs) at parity with an unconstrained build of the
-- same data: twin tables with identical rows isolate the spill path as the
-- only difference (the tier2_inline_vectors twin-table pattern).
--
-- Fixture: 3000 rows of vector(8) with maintenance_work_mem = 1MB.  The
-- graph accounting charges ~0.5 kB/node (node struct + vector copy +
-- neighbor array + visited slot), so the budget exhausts after ~2k rows
-- and the remaining rows are guaranteed to spill.  build_seed pins the
-- level sequence, so the WARNING numbers are deterministic.

\set ON_ERROR_STOP on
\set q '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'

CREATE SCHEMA test_tier2_mwm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_tier2_mwm, public;

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
FROM generate_series(1, 3000) i;

-- Twin table with identical rows (same physical order) for the control.
CREATE TABLE items_ctrl (
    id        int PRIMARY KEY,
    bucket    int,
    embedding vector(8)
);
INSERT INTO items_ctrl SELECT id, bucket, embedding FROM items ORDER BY id;

-- Build under a 1MB budget: succeeds, with exactly one spill WARNING.
SET maintenance_work_mem = '1MB';
SET pg_acorn.build_seed = 7;

CREATE INDEX items_mwm_idx ON items
    USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2,
          acorn_inline_vectors = true);

RESET maintenance_work_mem;

-- Control build of the same data, same seed, default budget: must NOT warn.
CREATE INDEX items_ctrl_idx ON items_ctrl
    USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2,
          acorn_inline_vectors = true);

RESET pg_acorn.build_seed;

-- All 3000 rows must be present in the spilled index.
SELECT c.reltuples::int AS index_tuples
FROM pg_class c WHERE c.relname = 'items_mwm_idx';

-- Exact ground truth: top-10 filtered KNN via seqscan (no index).
SET enable_seqscan = on;
SET enable_indexscan = off;
SET enable_bitmapscan = off;
CREATE TABLE truth AS
    SELECT id FROM items WHERE bucket < 1
    ORDER BY embedding <=> :'q'::vector LIMIT 10;
RESET enable_indexscan;
RESET enable_bitmapscan;

-- Recall against the exact truth table for either table's index.
CREATE OR REPLACE FUNCTION acorn_recall(tbl regclass, ef int) RETURNS numeric AS $$
DECLARE
    matched int;
BEGIN
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    EXECUTE format(
        'SELECT count(*) FROM (
             SELECT id FROM %s WHERE bucket < 1
             ORDER BY embedding <=> ''[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]''::vector
             LIMIT 10) r JOIN truth USING (id)', tbl)
    INTO matched;
    RESET enable_seqscan;
    RETURN matched / 10.0;
END;
$$ LANGUAGE plpgsql;

-- REAL recall parity: the spilled build within 0.2 of the unconstrained
-- control build at equal ef (the spill path is the only difference).
SELECT abs(acorn_recall('items', 400) - acorn_recall('items_ctrl', 400)) <= 0.2
    AS spill_recall_parity_ef400;

-- Filter correctness must hold on both graph regions (mem-built + spilled).
SET enable_seqscan = off;
SET pg_acorn.ef_search = 400;
SELECT bool_and(bucket < 5) AS filter_correct,
       count(*) = 10        AS returns_k
FROM (SELECT bucket FROM items WHERE bucket < 5
      ORDER BY embedding <=> :'q'::vector LIMIT 10) r;
RESET enable_seqscan;
RESET pg_acorn.ef_search;

DROP SCHEMA test_tier2_mwm CASCADE;
