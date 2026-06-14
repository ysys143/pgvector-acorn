"""Does pg_acorn.target_recall (auto-ef, borrow-list Priority 3) actually land
near the requested recall ACROSS selectivities WITHOUT manual ef tuning?

Auto-ef derives ef from the estimated filter selectivity (build-time histogram)
+ a recall target, via a coarse monotone heuristic (acorn_am.h).  This sweep
builds a histogram-bearing gamma=2 index on the correlated tv_items fixture and,
for each (target_recall, selectivity), measures ACHIEVED recall@10 with a
deliberately tiny manual ef_search=40 (which auto-ef must override) — vs the
target_recall=0 baseline (manual ef=40, no auto) to show the gap auto-ef closes.

Recall is deterministic, so host load is irrelevant.  The effective ef is read
back from pg_acorn_scan_stats (P5 telemetry).

Run inside the bench postgres container:
  python3 -u /workspace/bench/auto_ef_sweep.py
"""
import json
import os

import numpy as np
import psycopg

from thesis_validation import K, make_fixture, exact_truth, qstr, SQL

DSN = os.environ.get("PG_DSN",
                     "host=/var/run/postgresql dbname=bench user=postgres")
OUT = os.path.join(os.path.dirname(__file__), "results_auto_ef_sweep.json")
SELS = [1, 5, 10, 20]
TARGETS = [0.90, 0.95, 0.99]
IDX = "tv_acorn_autoef"
ALL_ACORN = ["tv_acorn_idx", "tv_acorn_noinline", "tv_acorn_g3", "tv_acorn_g4",
             IDX]


def build(cur):
    cur.execute(f"SELECT count(*) FROM pg_class WHERE relname = '{IDX}'")
    if cur.fetchone()[0]:
        print(f"[build] {IDX} exists, skip", flush=True)
        return
    print(f"[build] {IDX} (gamma=2, histogram) ...", flush=True)
    cur.execute("SET maintenance_work_mem = '2GB'")
    cur.execute("SET max_parallel_maintenance_workers = 4")
    cur.execute("SET pg_acorn.build_seed = 42")
    cur.execute("ALTER TABLE tv_items SET (parallel_workers = 4)")
    cur.execute(
        f"CREATE INDEX {IDX} ON tv_items "
        f"USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops) "
        f"WITH (m=16, ef_construction=64, acorn_gamma=2, "
        f"acorn_payload_edges=true, acorn_inline_vectors=false)")
    cur.execute(f"SELECT pg_size_pretty(pg_relation_size('{IDX}'))")
    print(f"[build] {IDX} done, size={cur.fetchone()[0]}", flush=True)


def measure(cur, queries, truths, sel):
    """Mean recall@10 over the query set + the effective ef of the last query."""
    recs = []
    for q in queries:
        cur.execute(SQL, (int(sel), qstr(q), K))
        ids = {r[0] for r in cur.fetchall()}
        recs.append(len(ids & truths[sel][len(recs)]) / K)
    # effective ef proxy: expansions of a single fresh scan
    cur.execute("SELECT pg_acorn_scan_stats_reset()")
    cur.execute(SQL, (int(sel), qstr(queries[0]), K))
    cur.fetchall()
    cur.execute("SELECT expansions FROM pg_acorn_scan_stats()")
    exp = int(cur.fetchone()[0])
    return float(np.mean(recs)), exp


def main():
    print("[fixture] correlated seed0 ...", flush=True)
    vecs, buckets, queries = make_fixture()
    truths = {s: [exact_truth(vecs, buckets, q, s) for q in queries] for s in SELS}

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    cur = conn.cursor()
    build(cur)

    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET pg_acorn.member_first = on")
    cur.execute("SET pg_acorn.scan_code_cache = on")
    cur.execute("SET pg_acorn.scan_inline_vectors = on")

    cur.execute("BEGIN")
    for idx in ALL_ACORN:
        if idx != IDX:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
    cur.execute("EXPLAIN (FORMAT TEXT) " + SQL, (10, qstr(queries[0]), K))
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert f"using {IDX}" in plan and "Index Cond" in plan, f"plan confound:\n{plan}"

    # warm the code cache
    cur.execute("SET pg_acorn.ef_search = 40")
    for q in queries:
        cur.execute(SQL, (10, qstr(q), K))
        cur.fetchall()

    out = {"meta": {"fixture": "correlated seed0 250K", "k": K, "index": IDX,
                    "note": "manual ef_search=40 throughout; auto-ef overrides it"},
           "baseline_manual_ef40": {}, "auto": {}}

    # baseline: target_recall = 0 (manual ef=40, no auto)
    cur.execute("SET pg_acorn.target_recall = 0")
    cur.execute("SET pg_acorn.ef_search = 40")
    for sel in SELS:
        rec, exp = measure(cur, queries, truths, sel)
        out["baseline_manual_ef40"][str(sel)] = {"recall": round(rec, 4), "ef_exp": exp}
        print(f"[manual ef40 sel={sel}%] recall={rec:.3f} exp={exp}", flush=True)

    # auto-ef: ef_search stays 40 but target_recall>0 overrides it
    for target in TARGETS:
        cur.execute(f"SET pg_acorn.target_recall = {target}")
        out["auto"][str(target)] = {}
        for sel in SELS:
            rec, exp = measure(cur, queries, truths, sel)
            out["auto"][str(target)][str(sel)] = {"recall": round(rec, 4), "ef_exp": exp}
            print(f"[auto target={target} sel={sel}%] recall={rec:.3f} "
                  f"ef~{exp}", flush=True)
    cur.execute("ROLLBACK")

    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    conn.close()
    print(f"\n[done] -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
