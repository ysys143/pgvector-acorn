#!/usr/bin/env python3
"""build_bench.py — V1 measurement: build-time fmgr vs direct kernels,
plus pg_acorn.build_seed determinism.

Config matches the W-series build-cost baseline: n=60K, dim=128, gamma=2,
acorn_payload_edges=on, single-threaded CREATE INDEX.

  1. build with pg_acorn.build_direct_dist = off  (fmgr path, pre-V1)
  2. build with pg_acorn.build_direct_dist = on   (direct C kernels, V1)
  3. determinism: build_seed=12345 twice -> identical top-10 ids on a fixed
     query set; build_seed=54321 -> different graph (different ids on at
     least one query)

Self-contained container run:
  docker run --rm -v "$PWD":/workspace -w /workspace pg_acorn_test \
      bash bench/run_audit.sh bench/build_bench.py
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import psycopg

K = 10
NQ = 40
DATA_SEED = 42


def qstr(v) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def make_fixture(n: int, dim: int):
    rng = np.random.default_rng(DATA_SEED)
    raw = rng.standard_normal((n, dim)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    blocks = dim // 10
    block_norms = np.array([
        np.linalg.norm(vecs[:, i * 10:(i + 1) * 10], axis=1)
        for i in range(blocks)
    ]).T
    dominant = np.argmax(block_norms, axis=1)
    span = 100 // blocks
    buckets = np.clip(dominant * span + rng.integers(0, span, size=n),
                      0, 99).astype(int)
    qraw = rng.standard_normal((NQ, dim)).astype(np.float32)
    queries = qraw / np.linalg.norm(qraw, axis=1, keepdims=True)
    return vecs, buckets, queries


def build(cur, *, direct: bool, seed: int, diversify: bool, n_label: str):
    cur.execute("DROP INDEX IF EXISTS bb_idx")
    cur.execute(f"SET pg_acorn.build_direct_dist = "
                f"{'on' if direct else 'off'}")
    cur.execute(f"SET pg_acorn.build_seed = {seed}")
    t0 = time.perf_counter()
    cur.execute(
        f"""CREATE INDEX bb_idx ON bb_items
            USING acorn_hnsw (embedding vector_cosine_ops,
                              bucket int4_acorn_ops)
            WITH (m = 16, ef_construction = 64, acorn_gamma = 2,
                  acorn_payload_edges = true,
                  acorn_diversify = {'true' if diversify else 'false'})""")
    dt = time.perf_counter() - t0
    print(f"[build] {n_label}: direct={direct} seed={seed} "
          f"diversify={diversify} -> {dt:.1f}s", flush=True)
    return dt


def topk_ids(cur, queries, ef=200):
    cur.execute("SET pg_acorn.enable_hook = off")
    cur.execute("SET enable_seqscan = off")
    cur.execute(f"SET pg_acorn.ef_search = {ef}")
    out = []
    for q in queries:
        cur.execute("SELECT id FROM bb_items "
                    "ORDER BY embedding <=> %s::vector LIMIT 10", (qstr(q),))
        out.append(tuple(r[0] for r in cur.fetchall()))
    cur.execute("RESET enable_seqscan")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost/postgres")
    ap.add_argument("--n", type=int, default=60_000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--diversify", action="store_true",
                    help="also time the diversify=on build")
    ap.add_argument("--out", default="bench/results_build_bench.json")
    args = ap.parse_args()

    vecs, buckets, queries = make_fixture(args.n, args.dim)
    conn = psycopg.connect(args.dsn, autocommit=True)

    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
        cur.execute("DROP TABLE IF EXISTS bb_items")
        cur.execute(f"CREATE TABLE bb_items (id serial PRIMARY KEY, "
                    f"bucket int, embedding vector({args.dim}))")
        t0 = time.perf_counter()
        with cur.copy("COPY bb_items (bucket, embedding) FROM STDIN") as cp:
            for i in range(args.n):
                cp.write_row((int(buckets[i]), qstr(vecs[i])))
        cur.execute("ANALYZE bb_items")
        print(f"[load] {args.n} rows in {time.perf_counter()-t0:.1f}s",
              flush=True)

        res = {"meta": vars(args), "builds": {}}

        # V1 A/B: fmgr vs direct kernels (diversify off = pre-Y2 baseline)
        res["builds"]["fmgr"] = build(cur, direct=False, seed=12345,
                                      diversify=False, n_label="A fmgr")
        res["builds"]["direct"] = build(cur, direct=True, seed=12345,
                                        diversify=False, n_label="B direct")
        res["speedup"] = round(res["builds"]["fmgr"]
                               / res["builds"]["direct"], 2)
        print(f"[V1] speedup fmgr/direct = {res['speedup']}x", flush=True)

        if args.diversify:
            res["builds"]["direct_diversify"] = build(
                cur, direct=True, seed=12345, diversify=True,
                n_label="C direct+diversify")

        # Determinism: same seed twice -> identical ids; different seed differs
        build(cur, direct=True, seed=12345, diversify=True, n_label="D1")
        ids_a = topk_ids(cur, queries)
        build(cur, direct=True, seed=12345, diversify=True, n_label="D2")
        ids_b = topk_ids(cur, queries)
        build(cur, direct=True, seed=54321, diversify=True, n_label="D3")
        ids_c = topk_ids(cur, queries)

        same = sum(a == b for a, b in zip(ids_a, ids_b))
        diff = sum(a != c for a, c in zip(ids_a, ids_c))
        res["determinism"] = {
            "same_seed_identical_queries": f"{same}/{NQ}",
            "diff_seed_differing_queries": f"{diff}/{NQ}",
            "same_seed_ok": same == NQ,
            "diff_seed_ok": diff > 0,
        }
        print(f"[determinism] same-seed identical {same}/{NQ}, "
              f"diff-seed differing {diff}/{NQ}", flush=True)

        with open(args.out, "w") as f:
            json.dump(res, f, indent=1)
        print(f"[done] -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
