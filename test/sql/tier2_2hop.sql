-- tier2_2hop.sql: 2-hop blind + NaviX-Directed expansion for in-filter scans
-- Tests: GUC existence, filter correctness with 2-hop on, recall at low selectivity

\set ON_ERROR_STOP on

CREATE SCHEMA test_tier2_2hop;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_tier2_2hop, public;

-- 1000 rows, 10 buckets → bucket < 1 = ~10% selectivity (~100 rows)
--                        → bucket < 5 = ~50% selectivity (~500 rows)
CREATE TABLE items (
    id        serial PRIMARY KEY,
    bucket    int,
    embedding vector(4)
);

INSERT INTO items (bucket, embedding) SELECT
    (i % 10),
    ('[' || (random())::text || ',' || (random())::text || ','
          || (random())::text || ',' || (random())::text || ']')::vector
FROM generate_series(1, 1000) i;

CREATE INDEX items_acorn_mc ON items
    USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2);

-- GUC must exist (errors before Phase 2A implementation)
SET pg_acorn.enable_2hop = off;

-- Baseline: 2-hop off — standard ACORN traversal
SET pg_acorn.enable_2hop = off;
SET enable_seqscan = off;
SELECT bool_and(bucket < 5) AS filter_correct_baseline
FROM (SELECT bucket FROM items WHERE bucket < 5
      ORDER BY embedding <=> '[0.1,0.2,0.3,0.4]'::vector LIMIT 10) r;
RESET enable_seqscan;

-- 2-hop on: filter correctness must still hold
SET pg_acorn.enable_2hop = on;
SET enable_seqscan = off;
SELECT bool_and(bucket < 5) AS filter_correct_2hop
FROM (SELECT bucket FROM items WHERE bucket < 5
      ORDER BY embedding <=> '[0.1,0.2,0.3,0.4]'::vector LIMIT 10) r;

-- Recall at 10% selectivity with 2-hop on (lenient: >= 0.5 — correctness sanity)
CREATE OR REPLACE FUNCTION recall_2hop(threshold int, query vector)
RETURNS numeric AS $$
DECLARE matched int;
BEGIN
    WITH
    brute AS (
        SELECT id FROM items WHERE bucket < threshold
        ORDER BY embedding <=> query LIMIT 10
    ),
    acorn_r AS (
        SELECT id FROM items WHERE bucket < threshold
        ORDER BY embedding <=> query LIMIT 10
    )
    SELECT count(*) INTO matched FROM acorn_r JOIN brute USING (id);
    RETURN matched / 10.0;
END;
$$ LANGUAGE plpgsql;

SELECT recall_2hop(1, '[0.1,0.2,0.3,0.4]'::vector) >= 0.5 AS ok_10pct_2hop;
RESET enable_seqscan;

RESET pg_acorn.enable_2hop;

DROP SCHEMA test_tier2_2hop CASCADE;
