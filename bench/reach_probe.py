#!/usr/bin/env python3
"""reach_probe.py — graph-only reachability probes at 50K (Y2 verdict).

The graph_audit FETCH=50 ceilings turned out to be bound by the t2 stream's
emission rule (post_ef400 == post_ef1600 exactly on all 20 cells), so they
cannot distinguish graph quality.  Two probes that can:

  (a) navigability: self point-queries (LIMIT 1) on a node sample — the first
      emission is the greedy descent's local minimum, so the miss fraction
      measures how often greedy navigation cannot reach an exact-match node
      (graph property; scan constant across variants).
  (b) forced exploration: unfiltered LIMIT 2000 fetch, re-rank by reported
      distance, recall@10 of the top 10 — each additional emission forces the
      frontier to drain further, so 2000 emissions approximate an ef-bounded
      search; unreachable true neighbors stay missing.

Run (container):
  docker run --rm -v "$PWD":/workspace -w /workspace pg_acorn_test \
      bash bench/run_audit.sh bench/reach_probe.py --n 50000 --dim 128
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import psycopg

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from graph_audit import (make_fixture, qstr, exact_truth, load_data,
                         build_index, session, K)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost/postgres")
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--nav-sample", type=int, default=1000)
    ap.add_argument("--fetch", type=int, default=2000)
    ap.add_argument("--out", default="bench/results_reach_probe.json")
    args = ap.parse_args()

    vecs, buckets, queries = make_fixture(args.n, args.dim)
    truths_unf = [exact_truth(vecs, buckets, q, None) for q in queries]

    conn = psycopg.connect(args.dsn, autocommit=True)
    load_data(conn, vecs, buckets, False)

    rng = np.random.default_rng(7)
    sample = rng.choice(args.n, args.nav_sample, replace=False)

    res = {"meta": vars(args), "cells": {}}
    for variant in ("off", "on"):
        with conn.cursor() as cur:
            bt = build_index(cur, args.seed, variant == "on", 64, True, 2)
            session(cur, member_first=False)
            cur.execute("SET pg_acorn.ef_search = 2000")

            # (a) navigability: self point-queries
            t0 = time.perf_counter()
            miss = 0
            for i in sample:
                cur.execute("SELECT id FROM audit_items "
                            "ORDER BY embedding <=> %s::vector LIMIT 1",
                            (qstr(vecs[i]),))
                if cur.fetchone()[0] != int(i) + 1:
                    miss += 1
            nav_t = time.perf_counter() - t0

            # (b) forced exploration: LIMIT fetch, rerank, recall@10
            t1 = time.perf_counter()
            recalls = []
            for qi, truth in enumerate(truths_unf):
                cur.execute(
                    "SELECT id, (embedding <=> %(q)s::vector) FROM audit_items "
                    "ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s",
                    {"q": qstr(queries[qi]), "k": args.fetch})
                rows = sorted(cur.fetchall(), key=lambda r: r[1])
                recalls.append(len({r[0] for r in rows[:K]} & truth) / K)
            fe_t = time.perf_counter() - t1

            cell = {"build_s": round(bt, 1),
                    "nav_invisible": f"{miss}/{args.nav_sample}",
                    "nav_invisible_frac": round(miss / args.nav_sample, 4),
                    "nav_probe_s": round(nav_t, 1),
                    f"unf_rr{args.fetch}@10": round(float(np.mean(recalls)), 4),
                    "fe_probe_s": round(fe_t, 1)}
            res["cells"][variant] = cell
            print(f"[probe] {variant}: {cell}", flush=True)
            with open(args.out, "w") as f:
                json.dump(res, f, indent=1)

    print(f"[done] -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
