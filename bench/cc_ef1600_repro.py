#!/usr/bin/env python3
"""Controlled ef=1600 cache-mode repro on a quiet host (250K).

Hammers the exact cell that crashed during the contended G2 runs
(sel=1% ef=1600, scan_code_cache=on, tv_acorn_noinline) to decide whether
the crash reproduces without host contention.  Prints its backend PID up
front so a gdb attach can catch the signal live if it does crash.

Run inside the bench postgres container:
  python3 -u /workspace/bench/cc_ef1600_repro.py [--iters 500] [--sels 1,10,20]
"""
import argparse
import sys
import time

import numpy as np
import psycopg

from thesis_validation import K, SQL, make_fixture, qstr

DSN = "host=/var/run/postgresql dbname=bench user=postgres"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=500)
    ap.add_argument("--ef", type=int, default=1600)
    ap.add_argument("--sels", default="1,10,20")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to sleep after printing PID (gdb attach)")
    args = ap.parse_args()
    sels = [int(s) for s in args.sels.split(",")]

    print("[fixture] regenerating ...", flush=True)
    vecs, buckets, queries = make_fixture()

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    cur = conn.cursor()
    cur.execute("SELECT pg_backend_pid()")
    pid = cur.fetchone()[0]
    print(f"[pid] backend = {pid}", flush=True)
    if args.pause > 0:
        print(f"[pause] {args.pause}s for gdb attach ...", flush=True)
        time.sleep(args.pause)

    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET enable_sort = off")
    cur.execute("SET pg_acorn.member_first = on")
    cur.execute("SET pg_acorn.scan_inline_vectors = on")
    cur.execute("SET pg_acorn.scan_code_cache = on")
    cur.execute(f"SET pg_acorn.ef_search = {args.ef}")

    # Force tv_acorn_noinline + cache: drop the inline index in-txn.
    cur.execute("BEGIN")
    cur.execute("DROP INDEX tv_acorn_idx")
    # plan check
    cur.execute("EXPLAIN (FORMAT TEXT) " + SQL,
                (int(sels[0]), qstr(queries[0]), K))
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert "tv_acorn_noinline" in plan and "Index Cond" in plan, plan
    print(f"[plan] {plan.splitlines()[1].strip()}", flush=True)

    n = 0
    t0 = time.perf_counter()
    for it in range(args.iters):
        for sel in sels:
            q = queries[(n) % len(queries)]
            cur.execute(SQL, (int(sel), qstr(q), K))
            cur.fetchall()
            n += 1
        if (it + 1) % 50 == 0:
            dt = time.perf_counter() - t0
            print(f"[ok] {n} queries, {it + 1}/{args.iters} iters, "
                  f"{dt:.1f}s", flush=True)
    cur.execute("ROLLBACK")
    print(f"[done] survived {n} queries at ef={args.ef}, "
          f"no crash", flush=True)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
