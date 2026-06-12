#!/usr/bin/env python3
"""Build tv_acorn_noinline on tv_items (250K) with 4 parallel workers.

Same session recipe as Z4's build_ops_bench.  Run inside the bench postgres
container:  python3 -u /workspace/bench/build_noinline_250k.py
"""
import threading
import time

import psycopg

DSN = "host=/var/run/postgresql dbname=bench user=postgres"


def watch(stop_evt, out):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        peak = 0
        while not stop_evt.is_set():
            cur.execute("SELECT count(*) FROM pg_stat_activity "
                        "WHERE backend_type = 'parallel worker'")
            peak = max(peak, cur.fetchone()[0])
            time.sleep(0.5)
        out["peak"] = peak


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET pg_acorn.enable_hook = off")
    cur.execute("SET pg_acorn.build_direct_dist = on")
    cur.execute("SET pg_acorn.build_seed = 42")
    cur.execute("SET maintenance_work_mem = '2GB'")
    cur.execute("SET max_parallel_maintenance_workers = 4")
    cur.execute("ALTER TABLE tv_items SET (parallel_workers = 4)")
    cur.execute("DROP INDEX IF EXISTS tv_acorn_noinline")

    stop_evt = threading.Event()
    out = {}
    t = threading.Thread(target=watch, args=(stop_evt, out))
    t.start()
    t0 = time.perf_counter()
    cur.execute("CREATE INDEX tv_acorn_noinline ON tv_items "
                "USING acorn_hnsw (embedding vector_cosine_ops, "
                "bucket int4_acorn_ops) "
                "WITH (m=16, ef_construction=64, acorn_gamma=2, "
                "acorn_payload_edges=true, acorn_inline_vectors=false)")
    dt = time.perf_counter() - t0
    stop_evt.set()
    t.join()
    cur.execute("SELECT pg_relation_size('tv_acorn_noinline')")
    size = cur.fetchone()[0]
    print(f"build_s={dt:.1f} peak_parallel_workers={out['peak']} "
          f"size_mb={size / 1024 / 1024:.0f}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
