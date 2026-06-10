-- tier2_payload_edges.sql: payload-aware neighbor edges for acorn_hnsw.
--
-- New reloption acorn_payload_edges (bool, default false).  When on, each
-- node's layer-0 neighbor slots are split: half global nearest (standard HNSW)
-- and half nearest among nodes sharing the same payload partition
-- (hash(filter_val) % 256; identity for small ints).  This makes the
-- predicate subgraph navigable on correlated data without raising gamma.
--
-- REAL recall test: exact seqscan ground truth (truth_* tables), then overlap
-- with the index result.  NOT the tautological recall_* CTE pattern.
--
-- Fixture: 5 spatial clusters x 10 sub-buckets (bucket = i % 50, cluster =
-- bucket / 10).  An equality filter (bucket = 0) selects a sparse random
-- subset of cluster 0, so without payload edges the passing nodes are poorly
-- connected through pure-distance neighbors.

\set ON_ERROR_STOP on
\set q '[0.15,0.15,0.15,0.15,0.15,0.15,0.15,0.15]'

CREATE SCHEMA test_t2_payload;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_t2_payload, public;

SELECT setseed(0.7);
CREATE TABLE items_off (
    id        serial PRIMARY KEY,
    bucket    int,
    embedding vector(8)
);

-- bucket 0..49; spatial cluster center = (bucket/10) * 0.6 on every dim,
-- noise U(0, 0.3).  Clusters are separated under L2; buckets within a
-- cluster are spatially interleaved.
INSERT INTO items_off (bucket, embedding)
SELECT b,
       ('[' || c + random()*0.3 || ',' || c + random()*0.3 || ','
             || c + random()*0.3 || ',' || c + random()*0.3 || ','
             || c + random()*0.3 || ',' || c + random()*0.3 || ','
             || c + random()*0.3 || ',' || c + random()*0.3 || ']')::vector
FROM (SELECT (i % 50) AS b, ((i % 50) / 10) * 0.6 AS c
      FROM generate_series(1, 2000) i) t;

-- Identical data for the payload-edges index (same ids, same vectors).
CREATE TABLE items_on AS SELECT * FROM items_off;

-- (a) reloption accepted; index builds with payload edges on and off.
CREATE INDEX items_off_idx ON items_off
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 8, ef_construction = 64, acorn_gamma = 1);
CREATE INDEX items_on_idx ON items_on
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 8, ef_construction = 64, acorn_gamma = 1, acorn_payload_edges = true);

SELECT reloptions FROM pg_class WHERE relname = 'items_on_idx';

-- (b) filter correctness with payload edges on: every returned row satisfies
-- the qual, for both equality and range.
SET enable_seqscan = off;
SET pg_acorn.ef_search = 100;
SELECT bool_and(bucket = 0) AS eq_filter_correct, count(*) = 10 AS eq_full_k
FROM (SELECT bucket FROM items_on WHERE bucket = 0
      ORDER BY embedding <-> :'q'::vector LIMIT 10) r;
SELECT bool_and(bucket < 3) AS range_filter_correct, count(*) = 10 AS range_full_k
FROM (SELECT bucket FROM items_on WHERE bucket < 3
      ORDER BY embedding <-> :'q'::vector LIMIT 10) r;
RESET enable_seqscan;

-- Exact ground truth via seqscan (REAL recall, not tautological).
SET enable_indexscan = off;
SET enable_bitmapscan = off;
CREATE TABLE truth_eq AS
    SELECT id FROM items_off WHERE bucket = 0
    ORDER BY embedding <-> :'q'::vector LIMIT 10;
CREATE TABLE truth_range AS
    SELECT id FROM items_off WHERE bucket < 3
    ORDER BY embedding <-> :'q'::vector LIMIT 10;
RESET enable_indexscan;
RESET enable_bitmapscan;

-- recall@10 for a given table / qual / truth table at a given ef_search.
CREATE OR REPLACE FUNCTION acorn_recall(tbl text, qual text, truth_tbl text, ef int)
RETURNS numeric AS $$
DECLARE
    matched int;
BEGIN
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    EXECUTE format(
        'WITH r AS (SELECT id FROM %I WHERE %s
                    ORDER BY embedding <-> ''[0.15,0.15,0.15,0.15,0.15,0.15,0.15,0.15]''::vector
                    LIMIT 10)
         SELECT count(*) FROM r JOIN %I t USING (id)', tbl, qual, truth_tbl)
        INTO matched;
    RESET enable_seqscan;
    RETURN matched / 10.0;
END;
$$ LANGUAGE plpgsql;

-- (c) REAL recall: payload edges on >= off on identical data at equal
-- ef_search, for equality and range quals.  Both indexes were built in this
-- session with the same level sequence, so the comparison is deterministic.
SELECT acorn_recall('items_on', 'bucket = 0', 'truth_eq', 40)
    >= acorn_recall('items_off', 'bucket = 0', 'truth_eq', 40)  AS eq_ge_ef40;
SELECT acorn_recall('items_on', 'bucket = 0', 'truth_eq', 200)
    >= acorn_recall('items_off', 'bucket = 0', 'truth_eq', 200) AS eq_ge_ef200;
SELECT acorn_recall('items_on', 'bucket < 3', 'truth_range', 40)
    >= acorn_recall('items_off', 'bucket < 3', 'truth_range', 40)  AS range_ge_ef40;
SELECT acorn_recall('items_on', 'bucket < 3', 'truth_range', 200)
    >= acorn_recall('items_off', 'bucket < 3', 'truth_range', 200) AS range_ge_ef200;

-- Payload edges must reach high absolute recall at a moderate budget on this
-- fixture (~40 passing rows for the equality qual).
SELECT acorn_recall('items_on', 'bucket = 0', 'truth_eq', 200) >= 0.8 AS eq_recall_floor;

-- (d) insert path: aminsert with payload edges on stays correct.
INSERT INTO items_on (bucket, embedding)
SELECT (i % 50),
       ('[' || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ','
             || ((i % 50) / 10) * 0.6 + random()*0.3 || ']')::vector
FROM generate_series(1, 100) i;

SET enable_seqscan = off;
SET pg_acorn.ef_search = 100;
SELECT bool_and(bucket = 0) AS insert_eq_filter_correct, count(*) = 10 AS insert_eq_full_k
FROM (SELECT bucket FROM items_on WHERE bucket = 0
      ORDER BY embedding <-> :'q'::vector LIMIT 10) r;
RESET enable_seqscan;
RESET pg_acorn.ef_search;

DROP SCHEMA test_t2_payload CASCADE;
