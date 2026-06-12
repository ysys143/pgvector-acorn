#!/usr/bin/env python3
"""emission_audit_slice.py — A/B the Tier 2 emission-order fix.

Reuses the graph_audit.py protocol verbatim (n=50K dim=128 correlated
fixture, deterministic build_seed, plan-checked cells) but measures each
cell under BOTH pg_acorn.buffered_emission settings on the SAME index
(one build per seed), so the comparison has no graph confound:

  eager (off)    legacy emission — emits the greedy local minimum as soon
                 as no unexpanded candidate is closer (measured ORDER BY
                 inversions; sel40_raw ~0.81 vs rr50 1.000 in
                 bench/results_graph_audit.json, 10 seeds)
  buffered (on)  fix — expansion phase runs to the full ef budget before
                 emission starts; emitted order is exact

Cells per seed and mode (raw@10 / rerank50@10, 40 queries):
  unf      unfiltered, ef=2000, member_first=off
  sel10/40 in-filter, ef=1600, member_first=on
  post20   postfilter ((bucket+0)<20 defeats pushdown), ef=400, mf=off

Gate: buffered sel40_raw jumps toward rr50 (0.81 -> ~1.0) at the same ef.

Self-contained container run (do NOT point at the compose stack):
  docker run --rm -v "$PWD":/workspace -w /workspace pg_acorn_test \
      bash bench/run_audit.sh bench/emission_audit_slice.py --seeds 11,22,33
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import psycopg

from graph_audit import (K, NQ, UNF_EF, FIL_EF, SELS, POST_SEL,
                         make_fixture, exact_truth, qstr, session,
                         plan_check, mean_recall, load_data, build_index,
                         SQL_UNF, SQL_FIL, SQL_POST)

POST_EF = 400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost/postgres")
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--efc", type=int, default=64)
    ap.add_argument("--gamma", type=int, default=2)
    ap.add_argument("--seeds", default="11,22,33")
    ap.add_argument("--diversify", default="off", choices=("off", "on"))
    ap.add_argument("--force-load", action="store_true")
    ap.add_argument("--out", default="bench/results_emission_audit.json")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    diversify = (args.diversify == "on")

    print(f"[fixture] n={args.n} dim={args.dim} corr=high", flush=True)
    vecs, buckets, queries = make_fixture(args.n, args.dim)
    truths_unf = [exact_truth(vecs, buckets, q, None) for q in queries]
    truths_fil = {s: [exact_truth(vecs, buckets, q, s) for q in queries]
                  for s in SELS}
    truths_post = [exact_truth(vecs, buckets, q, POST_SEL) for q in queries]

    conn = psycopg.connect(args.dsn, autocommit=True)
    load_data(conn, vecs, buckets, args.force_load)

    results = {"meta": vars(args), "cells": []}

    for seed in seeds:
        with conn.cursor() as cur:
            bt = build_index(cur, seed, diversify, args.efc, True,
                             args.gamma)
            cur.execute("ANALYZE audit_items")
            print(f"[build] seed={seed} {bt:.1f}s", flush=True)

            for mode in ("eager", "buffered"):
                buf = "on" if mode == "buffered" else "off"

                session(cur, member_first=False)
                cur.execute(f"SET pg_acorn.buffered_emission = {buf}")
                cur.execute(f"SET pg_acorn.ef_search = {UNF_EF}")
                plan_check(cur, SQL_UNF, {"q": qstr(queries[0])},
                           must=["Index Scan using audit_idx"])
                unf = mean_recall(cur, SQL_UNF,
                                  lambda qi: {"q": qstr(queries[qi])},
                                  truths_unf)

                session(cur, member_first=True)
                cur.execute(f"SET pg_acorn.buffered_emission = {buf}")
                cur.execute(f"SET pg_acorn.ef_search = {FIL_EF}")
                fil = {}
                for s in SELS:
                    plan_check(cur, SQL_FIL,
                               {"q": qstr(queries[0]), "sel": s},
                               must=["Index Scan using audit_idx",
                                     "Index Cond"])
                    fil[s] = mean_recall(
                        cur, SQL_FIL,
                        lambda qi, s=s: {"q": qstr(queries[qi]), "sel": s},
                        truths_fil[s])

                session(cur, member_first=False)
                cur.execute(f"SET pg_acorn.buffered_emission = {buf}")
                cur.execute(f"SET pg_acorn.ef_search = {POST_EF}")
                plan_check(cur, SQL_POST,
                           {"q": qstr(queries[0]), "sel": POST_SEL},
                           must=["Index Scan using audit_idx", "Filter:"],
                           must_not=["Index Cond"])
                post = mean_recall(
                    cur, SQL_POST,
                    lambda qi: {"q": qstr(queries[qi]), "sel": POST_SEL},
                    truths_post)

                cell = {"seed": seed, "mode": mode, "build_s": round(bt, 1),
                        "unf_raw": round(unf[0], 4),
                        "unf_rr50": round(unf[1], 4),
                        **{k: round(v, 4) for s in SELS
                           for k, v in ((f"sel{s}_raw", fil[s][0]),
                                        (f"sel{s}_rr50", fil[s][1]))},
                        f"post{POST_SEL}_ef{POST_EF}_raw": round(post[0], 4),
                        f"post{POST_SEL}_ef{POST_EF}_rr50": round(post[1], 4)}
                results["cells"].append(cell)
                print(f"[cell] {cell}", flush=True)
                with open(args.out, "w") as f:
                    json.dump(results, f, indent=1)

    print("\n=== summary (mean over seeds) ===", flush=True)
    for mode in ("eager", "buffered"):
        cells = [c for c in results["cells"] if c["mode"] == mode]
        keys = [k for k in cells[0] if k not in ("seed", "mode", "build_s")]
        print(f"{mode:>8}: " + "  ".join(
            f"{k}={np.mean([c[k] for c in cells]):.3f}" for k in keys),
            flush=True)
    print(f"[done] results -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
