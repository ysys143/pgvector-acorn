-- macorn_ab4.sql : CONTROLLED 3-way A/B at 200K, deterministic builds, wide ef.
--
-- Fixes ab2/ab3 confounds:
--   * SERIAL build (max_parallel_maintenance_workers=0) + build_seed=42 ->
--     deterministic graphs; plain vs macorn differ ONLY by the penalty.
--   * Wide ef sweep 10..400 so B3's high-ef regime is covered.
--   * Warm-up query pass before timing (kills cold-cache outliers).
--   * Ground truth via seqscan BEFORE indexes exist (no truth leak).
-- 200K keeps serial build affordable; per-bucket subgraph still fragments.
--
-- Run: sudo -u postgres psql -d bench -f macorn_ab4.sql

\set ON_ERROR_STOP on
\pset pager off
\timing off
SET maintenance_work_mem = '8GB';

DROP TABLE IF EXISTS mab_items CASCADE;
DROP TABLE IF EXISTS mab_queries CASCADE;
DROP TABLE IF EXISTS mab_truth CASCADE;
DROP TABLE IF EXISTS mab_results CASCADE;

CREATE TABLE mab_items AS SELECT id, (abs(hashint4(id)) % 100) AS bucket, embedding FROM tv_items WHERE id <= 200000;
ALTER TABLE mab_items ADD PRIMARY KEY (id);
CREATE TABLE mab_queries AS
    SELECT row_number() OVER (ORDER BY id) AS qid, id, bucket, embedding
    FROM tv_items WHERE id BETWEEN 1000001 AND 1100000 ORDER BY id LIMIT 100;

-- ground truth (seqscan, before indexes) --------------------------------------
SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_seqscan = on;
CREATE TABLE mab_truth(qid int, sel text, ids int[]);
INSERT INTO mab_truth SELECT q.qid, 'eq1', (SELECT array_agg(id) FROM (
        SELECT id FROM mab_items WHERE bucket = 7 ORDER BY embedding <=> q.embedding LIMIT 10) s) FROM mab_queries q;
INSERT INTO mab_truth SELECT q.qid, 'lt5', (SELECT array_agg(id) FROM (
        SELECT id FROM mab_items WHERE bucket < 5 ORDER BY embedding <=> q.embedding LIMIT 10) s) FROM mab_queries q;
RESET enable_indexscan; RESET enable_bitmapscan;

-- deterministic serial builds -------------------------------------------------
SET max_parallel_maintenance_workers = 0;
SET pg_acorn.build_seed = 42;

SET pg_acorn.build_macorn_penalty = off;
CREATE INDEX mab_plain_idx ON mab_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2, acorn_payload_edges = off);

SET pg_acorn.build_macorn_penalty = on; SET pg_acorn.build_macorn_penalty_factor = 1.0;
CREATE INDEX mab_macorn_idx ON mab_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2, acorn_payload_edges = off);
RESET pg_acorn.build_macorn_penalty; RESET pg_acorn.build_macorn_penalty_factor;

CREATE INDEX mab_b3_idx ON mab_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2, acorn_payload_edges = on, acorn_payload_m = 64);
RESET pg_acorn.build_seed; RESET max_parallel_maintenance_workers;

SELECT pg_size_pretty(pg_relation_size('mab_plain_idx')) AS plain_sz,
       pg_size_pretty(pg_relation_size('mab_macorn_idx')) AS macorn_sz,
       pg_size_pretty(pg_relation_size('mab_b3_idx')) AS b3_sz;

-- eval with warm-up pass ------------------------------------------------------
CREATE OR REPLACE FUNCTION mab_eval(idx regclass, sel_name text, qual text, ef int)
RETURNS TABLE(mean_recall numeric, mean_ms numeric) AS $$
DECLARE
    other regclass; q record; got int[]; truth int[]; inter int;
    rec_sum numeric := 0; ms_sum numeric := 0; n int := 0; t0 timestamptz; t1 timestamptz;
BEGIN
    FOR other IN SELECT indexrelid::regclass FROM pg_index
                 WHERE indrelid='mab_items'::regclass AND indexrelid<>idx LOOP
        EXECUTE format('UPDATE pg_index SET indisvalid=false WHERE indexrelid=%s', other::oid);
    END LOOP;
    EXECUTE format('SET pg_acorn.ef_search = %s', ef);
    SET enable_seqscan = off;
    -- warm-up: run every query once, discard timing/recall
    FOR q IN SELECT embedding FROM mab_queries LOOP
        EXECUTE format('SELECT 1 FROM (SELECT id FROM mab_items WHERE %s
                        ORDER BY embedding <=> $1 LIMIT 10) s', qual) USING q.embedding;
    END LOOP;
    -- timed pass
    FOR q IN SELECT qid, embedding FROM mab_queries LOOP
        SELECT ids INTO truth FROM mab_truth WHERE qid=q.qid AND sel=sel_name;
        t0 := clock_timestamp();
        EXECUTE format('SELECT array_agg(id) FROM (SELECT id FROM mab_items WHERE %s
                        ORDER BY embedding <=> $1 LIMIT 10) s', qual) INTO got USING q.embedding;
        t1 := clock_timestamp();
        SELECT count(*) INTO inter FROM unnest(truth) e WHERE e = ANY(got);
        rec_sum := rec_sum + inter::numeric/10;
        ms_sum  := ms_sum  + extract(epoch FROM (t1-t0))*1000; n := n+1;
    END LOOP;
    RESET enable_seqscan;
    UPDATE pg_index SET indisvalid=true WHERE indrelid='mab_items'::regclass;
    mean_recall := round(rec_sum/n,4); mean_ms := round(ms_sum/n,3); RETURN NEXT;
END $$ LANGUAGE plpgsql;

CREATE TABLE mab_results(idx text, sel text, ef int, mean_recall numeric, mean_ms numeric);
DO $$
DECLARE
    idxs text[][] := ARRAY[ARRAY['plain','mab_plain_idx'], ARRAY['macorn','mab_macorn_idx'], ARRAY['b3','mab_b3_idx']];
    efs int[] := ARRAY[10,20,30,50,75,100,150,200,300,400];
    sels text[][] := ARRAY[ARRAY['eq1','bucket = 7'], ARRAY['lt5','bucket < 5']];
    e int; r record;
BEGIN
    FOR k IN 1..array_length(idxs,1) LOOP
        FOR i IN 1..array_length(sels,1) LOOP
            FOREACH e IN ARRAY efs LOOP
                SELECT * INTO r FROM mab_eval(idxs[k][2]::regclass, sels[i][1], sels[i][2], e);
                INSERT INTO mab_results VALUES (idxs[k][1], sels[i][1], e, r.mean_recall, r.mean_ms);
            END LOOP;
        END LOOP;
    END LOOP;
END $$;

\echo
\echo ====== INDEPENDENT-FILTER 3-way (200K, serial+seed, warm): recall@10 ======
SELECT p.sel, p.ef, p.mean_recall AS plain, m.mean_recall AS macorn, b.mean_recall AS b3
FROM mab_results p
JOIN mab_results m ON m.sel=p.sel AND m.ef=p.ef AND m.idx='macorn'
JOIN mab_results b ON b.sel=p.sel AND b.ef=p.ef AND b.idx='b3'
WHERE p.idx='plain' ORDER BY p.sel, p.ef;
\echo
\echo ====== latency (ms) ======
SELECT p.sel, p.ef, p.mean_ms AS plain_ms, m.mean_ms AS macorn_ms, b.mean_ms AS b3_ms
FROM mab_results p
JOIN mab_results m ON m.sel=p.sel AND m.ef=p.ef AND m.idx='macorn'
JOIN mab_results b ON b.sel=p.sel AND b.ef=p.ef AND b.idx='b3'
WHERE p.idx='plain' ORDER BY p.sel, p.ef;

DROP TABLE mab_items CASCADE; DROP TABLE mab_queries CASCADE;
DROP TABLE mab_truth CASCADE; DROP TABLE mab_results CASCADE;
DROP FUNCTION mab_eval(regclass,text,text,int);
