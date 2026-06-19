-- macorn_ab6_10m.sql : plain vs B3 at ~10M, INDEPENDENT filter, wide ef.
--
-- Follow-up to the 200K finding: does B3 (acorn_payload_edges) widen its
-- high-ef advantage over plain as per-bucket subgraphs grow (~99K members at
-- 10M vs ~2K at 200K)? Independent filter (abs(hashint4(id))%100) forces the
-- fragmented regime. M-ACORN dropped (settled: inert). Parallel builds are fine
-- here -- plain vs B3 differ by config, not a subtle penalty, so build noise is
-- small vs the effect. Truth = seqscan before indexes. Queries held out
-- (id > 9.9M, not indexed). Warm-up pass before timing.
--
-- Run: nohup sudo -u postgres psql -d bench -f macorn_ab6_10m.sql > /tmp/ab6.log 2>&1 &

\set ON_ERROR_STOP on
\pset pager off
\timing off
SET max_parallel_maintenance_workers = 8;
SET maintenance_work_mem = '32GB';
SET max_parallel_workers = 8;
SET max_parallel_workers_per_gather = 4;

DROP TABLE IF EXISTS mab_items CASCADE;
DROP TABLE IF EXISTS mab_queries CASCADE;
DROP TABLE IF EXISTS mab_truth CASCADE;
DROP TABLE IF EXISTS mab_results CASCADE;

\echo >>> building 9.9M subset (independent filter)
CREATE TABLE mab_items AS
    SELECT id, (abs(hashint4(id)) % 100) AS bucket, embedding
    FROM tv_items WHERE id <= 9900000;
ALTER TABLE mab_items ADD PRIMARY KEY (id);
CREATE TABLE mab_queries AS
    SELECT row_number() OVER (ORDER BY id) AS qid, id, embedding
    FROM tv_items WHERE id > 9900000 ORDER BY id LIMIT 100;
SELECT count(*) AS n_items FROM mab_items;

\echo >>> ground truth (seqscan, before indexes)
SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_seqscan = on;
CREATE TABLE mab_truth(qid int, sel text, ids int[]);
INSERT INTO mab_truth SELECT q.qid, 'eq1', (SELECT array_agg(id) FROM (
        SELECT id FROM mab_items WHERE bucket = 7 ORDER BY embedding <=> q.embedding LIMIT 10) s) FROM mab_queries q;
INSERT INTO mab_truth SELECT q.qid, 'lt5', (SELECT array_agg(id) FROM (
        SELECT id FROM mab_items WHERE bucket < 5 ORDER BY embedding <=> q.embedding LIMIT 10) s) FROM mab_queries q;
RESET enable_indexscan; RESET enable_bitmapscan;

\echo >>> building plain index (payload_edges off)
SELECT clock_timestamp() AS t \gset
CREATE INDEX mab_plain_idx ON mab_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2, acorn_payload_edges = off);
SELECT 'plain' AS idx, round(extract(epoch FROM clock_timestamp()-:'t'::timestamptz)::numeric,1) AS build_s;

\echo >>> building B3 index (payload_edges on, payload_m 64)
SELECT clock_timestamp() AS t \gset
CREATE INDEX mab_b3_idx ON mab_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
    WITH (m = 16, ef_construction = 64, acorn_gamma = 2, acorn_payload_edges = on, acorn_payload_m = 64);
SELECT 'b3' AS idx, round(extract(epoch FROM clock_timestamp()-:'t'::timestamptz)::numeric,1) AS build_s;

SELECT pg_size_pretty(pg_relation_size('mab_plain_idx')) AS plain_sz,
       pg_size_pretty(pg_relation_size('mab_b3_idx')) AS b3_sz;

\echo >>> eval
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
    FOR q IN SELECT embedding FROM mab_queries LOOP    -- warm-up
        EXECUTE format('SELECT 1 FROM (SELECT id FROM mab_items WHERE %s
                        ORDER BY embedding <=> $1 LIMIT 10) s', qual) USING q.embedding;
    END LOOP;
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
    idxs text[][] := ARRAY[ARRAY['plain','mab_plain_idx'], ARRAY['b3','mab_b3_idx']];
    efs int[] := ARRAY[10,30,50,100,200,400,800];
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
\echo ====== plain vs B3 (9.9M, INDEPENDENT filter, warm): recall@10 + latency ======
SELECT p.sel, p.ef,
       p.mean_recall AS plain_rec, b.mean_recall AS b3_rec,
       round(b.mean_recall-p.mean_recall,4) AS d_rec,
       p.mean_ms AS plain_ms, b.mean_ms AS b3_ms
FROM mab_results p JOIN mab_results b ON b.sel=p.sel AND b.ef=p.ef AND b.idx='b3'
WHERE p.idx='plain' ORDER BY p.sel, p.ef;

DROP TABLE mab_items CASCADE; DROP TABLE mab_queries CASCADE;
DROP TABLE mab_truth CASCADE; DROP TABLE mab_results CASCADE;
DROP FUNCTION mab_eval(regclass,text,text,int);
