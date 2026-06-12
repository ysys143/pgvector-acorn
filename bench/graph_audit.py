#!/usr/bin/env python3
"""graph_audit.py — Y2 connectivity audit: diversity heuristic vs baseline.

Builds the acorn_hnsw index on CORRELATED data (thesis fixture scheme:
bucket derived from the dominant 10-dim vector block with within-block
spread, so bucket < k selects ~k% of rows) across many pg_acorn.build_seed
values and measures, per seed and per acorn_diversify variant:

  (a) unfiltered recall@10 at ef=2000 vs numpy truth
      -> reachability proxy: persistent < 1.0 = layer-0 fragmentation
  (b) filtered recall ceiling at sel=10% and 40%, ef=1600, member_first=on
      (thesis in-filter configuration)
  (c) optional postfilter-recovery cell: unfiltered stream + executor filter
      at sel=20% ((bucket + 0) < 20 defeats pushdown), member_first=off

Each cell reports two numbers:
  raw  = recall@10 of the first 10 rows the scan emits (end-to-end), and
  rr50 = recall@10 after re-ranking the scan's top-50 rows by true distance.
rr50 isolates GRAPH reachability from the t2 stream's emission-order
artifact: acorn_t2_stream_next emits the nearest discovered result as soon
as no unexpanded candidate is closer, so the greedy descent's local minimum
is emitted first even when later exploration finds closer nodes (ORDER BY
contract violation, measured rank-0 inversions; owned by the scan-side
workstream).  Diversity (this workstream) is judged on rr50.

Baseline (acorn_diversify=off) first, then diversify=on.  W1 documented
baseline failures: fragmentation at 12-15% of PID-derived seeds; sel=40%
ceiling 0.85-0.94.  Targets with diversify: unfiltered ~1.00 on ALL seeds,
sel=40% ceiling >= 0.99.

Self-contained container run:
  docker run --rm -v "$PWD":/workspace -w /workspace pg_acorn_test \
      bash bench/run_audit.sh bench/graph_audit.py --n 50000 --dim 128

Plans are EXPLAIN-checked once per cell kind (force-index confound guard).
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
UNF_EF = 2000
FIL_EF = 1600
SELS = (10, 40)
POST_SEL = 20
POST_EFS = (400, 1600)


def make_fixture(n: int, dim: int):
    """Thesis-validation correlated fixture (refined synthetic 'high')."""
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
    buckets = dominant * span + rng.integers(0, span, size=n)
    buckets = np.clip(buckets, 0, 99).astype(int)
    qraw = rng.standard_normal((NQ, dim)).astype(np.float32)
    queries = qraw / np.linalg.norm(qraw, axis=1, keepdims=True)
    return vecs, buckets, queries


def qstr(v) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def exact_truth(vecs, buckets, q, thresh):
    """ids (1-based) of the exact filtered top-K by cosine distance."""
    idx = np.where(buckets < thresh)[0] if thresh is not None \
        else np.arange(len(vecs))
    sims = vecs[idx] @ q
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())          # serial id = row index + 1


def session(cur, member_first: bool):
    cur.execute("SET pg_acorn.enable_hook = off")
    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET enable_sort = off")
    cur.execute(f"SET pg_acorn.member_first = {'on' if member_first else 'off'}")
    cur.execute("SET pg_acorn.scan_direct_dist = on")
    cur.execute("SET pg_acorn.scan_single_read = on")
    cur.execute("SET pg_acorn.scan_visited_oneprobe = on")
    cur.execute("SET pg_acorn.scan_direct_filter = on")
    cur.execute("SET pg_acorn.scan_prefetch = off")


def plan_check(cur, sql, params, must, must_not=()):
    cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
    plan = "\n".join(r[0] for r in cur.fetchall())
    for pat in must:
        assert pat in plan, f"plan check failed: {pat!r} missing\n{plan}"
    for pat in must_not:
        assert pat not in plan, f"plan check failed: {pat!r} present\n{plan}"


FETCH = 50
SQL_UNF = ("SELECT id, (embedding <=> %(q)s::vector) FROM audit_items "
           f"ORDER BY embedding <=> %(q)s::vector LIMIT {FETCH}")
SQL_FIL = ("SELECT id, (embedding <=> %(q)s::vector) FROM audit_items "
           "WHERE bucket < %(sel)s::int4 "
           f"ORDER BY embedding <=> %(q)s::vector LIMIT {FETCH}")
SQL_POST = ("SELECT id, (embedding <=> %(q)s::vector) FROM audit_items "
            "WHERE (bucket + 0) < %(sel)s::int4 "
            f"ORDER BY embedding <=> %(q)s::vector LIMIT {FETCH}")


def mean_recall(cur, sql, params_fn, truths):
    """(raw@10, rerank50@10) means over the query set."""
    raw, rr = [], []
    for qi, truth in enumerate(truths):
        cur.execute(sql, params_fn(qi))
        rows = cur.fetchall()
        raw.append(len({r[0] for r in rows[:K]} & truth) / K)
        rows = sorted(rows, key=lambda r: r[1])
        rr.append(len({r[0] for r in rows[:K]} & truth) / K)
    return float(np.mean(raw)), float(np.mean(rr))


def load_data(conn, vecs, buckets, force):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
        cur.execute("SELECT count(*) FROM pg_tables "
                    "WHERE tablename = 'audit_items'")
        if cur.fetchone()[0] == 1 and not force:
            cur.execute("SELECT count(*) FROM audit_items")
            if cur.fetchone()[0] == len(vecs):
                print(f"[load] reusing audit_items ({len(vecs)} rows)",
                      flush=True)
                return
        cur.execute("DROP TABLE IF EXISTS audit_items")
        dim = vecs.shape[1]
        cur.execute(f"CREATE TABLE audit_items (id serial PRIMARY KEY, "
                    f"bucket int, embedding vector({dim}))")
        t0 = time.perf_counter()
        with cur.copy("COPY audit_items (bucket, embedding) FROM STDIN") as cp:
            for i in range(len(vecs)):
                cp.write_row((int(buckets[i]), qstr(vecs[i])))
        cur.execute("ANALYZE audit_items")
        print(f"[load] {len(vecs)} rows in {time.perf_counter()-t0:.1f}s",
              flush=True)


def build_index(cur, seed: int, diversify: bool, efc: int,
                payload_edges: bool, gamma: int = 2):
    cur.execute("DROP INDEX IF EXISTS audit_idx")
    cur.execute(f"SET pg_acorn.build_seed = {seed}")
    t0 = time.perf_counter()
    cur.execute(
        f"""CREATE INDEX audit_idx ON audit_items
            USING acorn_hnsw (embedding vector_cosine_ops,
                              bucket int4_acorn_ops)
            WITH (m = 16, ef_construction = {efc}, acorn_gamma = {gamma},
                  acorn_payload_edges = {'true' if payload_edges else 'false'},
                  acorn_diversify = {'true' if diversify else 'false'})""")
    return time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost/postgres")
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--efc", type=int, default=64)
    ap.add_argument("--gamma", type=int, default=2)
    ap.add_argument("--seeds", default="11,22,33,44,55,66,77,88,99,110")
    ap.add_argument("--variants", default="off,on")
    ap.add_argument("--postfilter", action="store_true",
                    help="also measure the postfilter-recovery cell")
    ap.add_argument("--payload-edges", default="on", choices=("on", "off"))
    ap.add_argument("--force-load", action="store_true")
    ap.add_argument("--out", default="bench/results_graph_audit.json")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    variants = args.variants.split(",")

    print(f"[fixture] n={args.n} dim={args.dim} corr=high "
          f"data_seed={DATA_SEED}", flush=True)
    vecs, buckets, queries = make_fixture(args.n, args.dim)
    sel_frac = {s: float(np.mean(buckets < s)) for s in SELS + (POST_SEL,)}
    print(f"[fixture] selectivity fractions: {sel_frac}", flush=True)

    truths_unf = [exact_truth(vecs, buckets, q, None) for q in queries]
    truths_fil = {s: [exact_truth(vecs, buckets, q, s) for q in queries]
                  for s in SELS}
    truths_post = [exact_truth(vecs, buckets, q, POST_SEL) for q in queries]

    conn = psycopg.connect(args.dsn, autocommit=True)
    load_data(conn, vecs, buckets, args.force_load)

    results = {"meta": vars(args) | {"sel_frac": sel_frac}, "cells": []}

    for variant in variants:
        diversify = (variant == "on")
        for seed in seeds:
            with conn.cursor() as cur:
                bt = build_index(cur, seed, diversify, args.efc,
                                 args.payload_edges == "on", args.gamma)
                cur.execute("ANALYZE audit_items")
                session(cur, member_first=False)
                cur.execute(f"SET pg_acorn.ef_search = {UNF_EF}")
                plan_check(cur, SQL_UNF, {"q": qstr(queries[0])},
                           must=["Index Scan using audit_idx"])
                unf = mean_recall(cur, SQL_UNF,
                                  lambda qi: {"q": qstr(queries[qi])},
                                  truths_unf)

                session(cur, member_first=True)
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

                post = {}
                if args.postfilter:
                    session(cur, member_first=False)
                    for ef in POST_EFS:
                        cur.execute(f"SET pg_acorn.ef_search = {ef}")
                        plan_check(cur, SQL_POST,
                                   {"q": qstr(queries[0]), "sel": POST_SEL},
                                   must=["Index Scan using audit_idx",
                                         "Filter:"],
                                   must_not=["Index Cond"])
                        post[ef] = mean_recall(
                            cur, SQL_POST,
                            lambda qi: {"q": qstr(queries[qi]),
                                        "sel": POST_SEL},
                            truths_post)

                cell = {"variant": variant, "seed": seed,
                        "build_s": round(bt, 1),
                        "unf_raw": round(unf[0], 4),
                        "unf_rr50": round(unf[1], 4),
                        **{k: round(v, 4) for s in SELS
                           for k, v in ((f"sel{s}_raw", fil[s][0]),
                                        (f"sel{s}_rr50", fil[s][1]))},
                        **{k: round(v, 4) for ef, pv in post.items()
                           for k, v in ((f"post{POST_SEL}_ef{ef}_raw", pv[0]),
                                        (f"post{POST_SEL}_ef{ef}_rr50", pv[1]))}}
                results["cells"].append(cell)
                print(f"[cell] {cell}", flush=True)
                with open(args.out, "w") as f:
                    json.dump(results, f, indent=1)

    # summary
    print("\n=== summary (mean / min over seeds) ===", flush=True)
    for variant in variants:
        cells = [c for c in results["cells"] if c["variant"] == variant]
        if not cells:
            continue
        keys = [k for k in cells[0] if k not in ("variant", "seed", "build_s")]
        line = f"{variant:>4}: " + "  ".join(
            f"{k}={np.mean([c[k] for c in cells]):.3f}"
            f"/min {np.min([c[k] for c in cells]):.3f}" for k in keys)
        print(line, flush=True)
    print(f"[done] results -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
