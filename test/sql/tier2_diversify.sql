-- tier2_diversify.sql: HNSW diversity heuristic (acorn_diversify reloption)
-- and deterministic builds (pg_acorn.build_seed GUC).
--
-- Fixture: 10 well-separated clusters in 8 dims, bucket == cluster id, so the
-- filter and the vector space are perfectly correlated — the adversarial case
-- where nearest-only neighbor selection wires every node exclusively into its
-- own cluster and layer 0 fragments (W1 root cause).  All data is generated
-- with deterministic arithmetic (no RNG) and the index level RNG is pinned via
-- pg_acorn.build_seed, so every number below is reproducible.
--
-- Recall is REAL: measured against an exact seqscan truth table built before
-- any index exists (never the tautological recall_* CTE pattern).

\set ON_ERROR_STOP on

CREATE SCHEMA test_tier2_div;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_acorn;
SET search_path = test_tier2_div, public;

-- ---------------------------------------------------------------- fixture
-- cluster center: center(c, d) = 5 * sin(c * 1.9 + d * 2.7)
-- point i:        center(i % 10, d) + 0.3 * sin(i * 0.7718 + d * 2.31)
CREATE TABLE items (
    id        serial PRIMARY KEY,
    bucket    int,
    embedding vector(8)
);

INSERT INTO items (bucket, embedding)
SELECT i % 10,
       (SELECT ('[' || string_agg(
                   (5.0 * sin((i % 10) * 1.9 + d * 2.7)
                    + 0.3 * sin(i * 0.7718 + d * 2.31))::numeric(12, 6)::text,
                   ',' ORDER BY d) || ']')::vector
        FROM generate_series(0, 7) d)
FROM generate_series(1, 2000) i;

-- one query near each cluster center (constant 0.15 offset)
CREATE TABLE queries AS
SELECT c AS qid,
       (SELECT ('[' || string_agg(
                   (5.0 * sin(c * 1.9 + d * 2.7) + 0.15)::numeric(12, 6)::text,
                   ',' ORDER BY d) || ']')::vector
        FROM generate_series(0, 7) d) AS q
FROM generate_series(0, 9) c;

-- ---------------------------------------------------------------- truth
-- Exact ground truth via seqscan, built BEFORE any index exists.
CREATE TABLE truth_unf AS
SELECT q.qid, t.id
FROM queries q,
     LATERAL (SELECT id FROM items
              ORDER BY embedding <-> q.q LIMIT 10) t;

CREATE TABLE truth_fil AS
SELECT q.qid, t.id
FROM queries q,
     LATERAL (SELECT id FROM items WHERE bucket < 3
              ORDER BY embedding <-> q.q LIMIT 10) t;

-- ---------------------------------------------------------------- helpers
-- mean recall@10 over the 10 queries, unfiltered
CREATE OR REPLACE FUNCTION avg_recall_unf(ef int) RETURNS numeric AS $$
DECLARE
    total   numeric := 0;
    matched int;
    r       record;
BEGIN
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    FOR r IN SELECT qid, q FROM queries ORDER BY qid LOOP
        SELECT count(*) INTO matched
        FROM (SELECT id FROM items
              ORDER BY embedding <-> r.q LIMIT 10) a
        JOIN truth_unf t ON t.qid = r.qid AND t.id = a.id;
        total := total + matched / 10.0;
    END LOOP;
    RESET enable_seqscan;
    RETURN round(total / 10, 3);
END $$ LANGUAGE plpgsql;

-- mean recall@10 over the 10 queries, filtered (bucket < 3, 30% selectivity)
CREATE OR REPLACE FUNCTION avg_recall_fil(ef int) RETURNS numeric AS $$
DECLARE
    total   numeric := 0;
    matched int;
    r       record;
BEGIN
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    FOR r IN SELECT qid, q FROM queries ORDER BY qid LOOP
        SELECT count(*) INTO matched
        FROM (SELECT id FROM items WHERE bucket < 3
              ORDER BY embedding <-> r.q LIMIT 10) a
        JOIN truth_fil t ON t.qid = r.qid AND t.id = a.id;
        total := total + matched / 10.0;
    END LOOP;
    RESET enable_seqscan;
    RETURN round(total / 10, 3);
END $$ LANGUAGE plpgsql;

-- ordered top-10 ids for every query at a fixed ef (determinism check)
CREATE OR REPLACE FUNCTION all_topk(ef int) RETURNS TABLE (qid int, ids int[]) AS $$
DECLARE
    r record;
BEGIN
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    FOR r IN SELECT queries.qid AS i, q FROM queries ORDER BY queries.qid LOOP
        qid := r.i;
        ids := ARRAY(SELECT id FROM items
                     ORDER BY embedding <-> r.q LIMIT 10);
        RETURN NEXT;
    END LOOP;
    RESET enable_seqscan;
END $$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------- baseline
-- GUC must exist and be settable
SET pg_acorn.build_seed = 7;
SHOW pg_acorn.build_seed;

-- reloption must be accepted (off = legacy nearest-only selection)
CREATE INDEX items_acorn_div ON items
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 8, ef_construction = 64, acorn_gamma = 2,
          acorn_payload_edges = true, acorn_diversify = false);

CREATE TABLE scores (variant text, unf numeric, fil numeric);
INSERT INTO scores SELECT 'off', avg_recall_unf(80), avg_recall_fil(400);

DROP INDEX items_acorn_div;

-- ---------------------------------------------------------------- diversify
SET pg_acorn.build_seed = 7;
CREATE INDEX items_acorn_div ON items
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 8, ef_construction = 64, acorn_gamma = 2,
          acorn_payload_edges = true, acorn_diversify = true);

SELECT reloptions FROM pg_class WHERE relname = 'items_acorn_div';

INSERT INTO scores SELECT 'on', avg_recall_unf(80), avg_recall_fil(400);

-- Filter correctness with diversify on
SET enable_seqscan = off;
SET pg_acorn.ef_search = 80;
SELECT bool_and(bucket < 3) AS filter_correct
FROM (SELECT bucket FROM items WHERE bucket < 3
      ORDER BY embedding <-> (SELECT q FROM queries WHERE qid = 0)
      LIMIT 10) r;
RESET enable_seqscan;

-- REAL recall: diversify=on must not lose to off at equal ef.  Unfiltered
-- recall must be near-exact with diversify on (nearest-only selection
-- fragments layer 0 on this fixture: observed 0.700 off vs 1.000 on).
-- Filtered recall on this extreme island geometry stays budget/reachability
-- limited for queries whose passing clusters are far away (observed 0.300
-- off vs 0.400 on at ef=400); assert the ordering and an absolute floor,
-- not exactness — the 50K audit (bench/graph_audit.py) covers the realistic
-- correlated case.
SELECT (SELECT unf FROM scores WHERE variant = 'on')
    >= (SELECT unf FROM scores WHERE variant = 'off') AS unf_not_worse;
SELECT (SELECT fil FROM scores WHERE variant = 'on')
    >= (SELECT fil FROM scores WHERE variant = 'off') AS fil_not_worse;
SELECT (SELECT unf FROM scores WHERE variant = 'on') >= 0.95 AS unf_near_exact;
SELECT (SELECT fil FROM scores WHERE variant = 'on') >= 0.40 AS fil_floor;

-- ---------------------------------------------------------------- determinism
-- Same build_seed twice -> identical graph -> identical result ids
CREATE TABLE run1 AS SELECT * FROM all_topk(80);

DROP INDEX items_acorn_div;
SET pg_acorn.build_seed = 7;
CREATE INDEX items_acorn_div ON items
    USING acorn_hnsw (embedding vector_l2_ops, bucket int4_acorn_ops)
    WITH (m = 8, ef_construction = 64, acorn_gamma = 2,
          acorn_payload_edges = true, acorn_diversify = true);

CREATE TABLE run2 AS SELECT * FROM all_topk(80);

SELECT bool_and(r1.ids = r2.ids) AS same_seed_identical
FROM run1 r1 JOIN run2 r2 USING (qid);

RESET pg_acorn.build_seed;
RESET pg_acorn.ef_search;

DROP SCHEMA test_tier2_div CASCADE;
