#!/usr/bin/env python3
"""Re-measure the sel=10/20 money cells on a quiet host.

The full acceptance sweep (bench/results_emission_250k.json) ran while Z4
benches co-ran on this host: recalls are deterministic (unaffected) but the
medians show non-monotonic contention noise (sel=1 ef=200 med 40ms vs ef=400
med 6ms at perfectly monotonic buffer counts).  This script re-times ONLY the
verdict cells against same-session prefilter bars, reusing the surviving
tv_acorn_idx (g2 + payload_edges + inline, build_seed=42) and tv_bucket_btree.

Protocol mirrors thesis_validation.py FAIR-1T: same fixture/queries/truths,
unmeasured prewarm pass + NWARM, per-query latencies over PASSES timed passes
(median over all samples; per-query min reported), EXPLAIN plan checks.
"""

import json
import os
import time

import numpy as np
import psycopg

from thesis_validation import (K, NWARM, SQL, make_fixture, exact_truth,
                               qstr)

DSN = os.environ.get("PG_DSN", "postgresql://postgres:postgres@postgres/bench")
OUT = os.path.join(os.path.dirname(__file__), "results_emission_250k_quiet.json")
PASSES = 3
CELLS_ACORN = {10: [400, 800, 1600], 20: [800, 1600]}
SELS = [10, 20]


def plan_text(cur, sql, sel, q):
    cur.execute("EXPLAIN (FORMAT TEXT) " + sql, (int(sel), qstr(q), K))
    return "\n".join(r[0] for r in cur.fetchall())


def measure(cur, queries, truth, sel):
    for q in queries:                       # unmeasured prewarm pass
        cur.execute(SQL, (int(sel), qstr(q), K))
        cur.fetchall()
    for _ in range(NWARM):
        cur.execute(SQL, (int(sel), qstr(queries[0]), K))
        cur.fetchall()
    lats = np.empty((PASSES, len(queries)))
    recalls = []
    for p in range(PASSES):
        for qi, q in enumerate(queries):
            t0 = time.perf_counter()
            cur.execute(SQL, (int(sel), qstr(q), K))
            ids = {r[0] for r in cur.fetchall()}
            lats[p, qi] = (time.perf_counter() - t0) * 1e3
            if p == 0:
                recalls.append(len(ids & truth[qi]) / K)
    return lats, float(np.mean(recalls))


def main():
    print("[fixture] regenerating (deterministic) ...", flush=True)
    vecs, buckets, queries = make_fixture()
    truths = {s: [exact_truth(vecs, buckets, q, s) for q in queries]
              for s in SELS}

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    out = {"meta": {"protocol": "FAIR-1T re-measure, quiet host, "
                    f"passes={PASSES}, nq={len(queries)}",
                    "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())},
           "ops": []}

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE "
                    "indexname='tv_acorn_idx'")
        assert cur.fetchone()[0] == 1, "tv_acorn_idx missing — rebuild needed"

        # ---- prefilter bars (acorn index masked via enable_indexscan) ----
        cur.execute("SET pg_acorn.enable_hook = off")
        cur.execute("SET enable_indexscan = off")
        for sel in SELS:
            plan = plan_text(cur, SQL, sel, queries[0])
            assert "tv_acorn_idx" not in plan and "tv_hnsw_idx" not in plan, \
                f"prefilter plan confound:\n{plan}"
            lats, rec = measure(cur, queries, truths[sel], sel)
            op = {"engine": "pg_prefilter", "sel": sel, "ef": None,
                  "recall": rec, "med_ms": float(np.median(lats)),
                  "p90_ms": float(np.percentile(lats, 90)),
                  "min_mean_ms": float(lats.min(axis=0).mean()),
                  "plan_head": plan.splitlines()[0].strip()}
            out["ops"].append(op)
            print(f"[pg_prefilter] sel={sel}% recall={rec:.3f} "
                  f"med={op['med_ms']:.2f}ms p90={op['p90_ms']:.2f}ms "
                  f"min_mean={op['min_mean_ms']:.2f}ms", flush=True)
        cur.execute("RESET enable_indexscan")

        # ---- acorn money cells (buffered emission default on) ----
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_sort = off")
        cur.execute("SET pg_acorn.member_first = on")
        cur.execute("SET pg_acorn.scan_inline_vectors = on")
        cur.execute("SHOW pg_acorn.buffered_emission")
        assert cur.fetchone()[0] == "on"
        for sel in SELS:
            for ef in CELLS_ACORN[sel]:
                cur.execute(f"SET pg_acorn.ef_search = {ef}")
                plan = plan_text(cur, SQL, sel, queries[0])
                assert ("Index Scan using tv_acorn_idx" in plan
                        and "Index Cond" in plan), \
                    f"acorn plan confound:\n{plan}"
                lats, rec = measure(cur, queries, truths[sel], sel)
                op = {"engine": "acorn_g2pe_mf_inline", "sel": sel, "ef": ef,
                      "recall": rec, "med_ms": float(np.median(lats)),
                      "p90_ms": float(np.percentile(lats, 90)),
                      "min_mean_ms": float(lats.min(axis=0).mean()),
                      "buffered_emission": True}
                out["ops"].append(op)
                print(f"[acorn_g2_inline] sel={sel}% ef={ef} "
                      f"recall={rec:.3f} med={op['med_ms']:.2f}ms "
                      f"p90={op['p90_ms']:.2f}ms "
                      f"min_mean={op['min_mean_ms']:.2f}ms", flush=True)

    out["meta"]["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    print(f"[done] -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
