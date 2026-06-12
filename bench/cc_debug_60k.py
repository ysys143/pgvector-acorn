#!/usr/bin/env python3
"""M1.5 repro harness: 60K fixture, ef sweep, cache vs inline vs off.

Run inside the cc debug container:
    python3 -u /workspace/bench/cc_debug_60k.py build   # fixture + indexes
    python3 -u /workspace/bench/cc_debug_60k.py sweep   # ef sweep, all modes

Mirrors the bench protocol (prepare_threshold=0, member_first=on, DROP INDEX
of the competing index inside a transaction, medians over repeats).
"""
import json
import statistics
import sys
import time

import psycopg

DSN = "host=/var/run/postgresql dbname=postgres user=postgres"
N = 60000
DIM = 128
EFS = [100, 200, 400, 800, 1600]
REPS = 15        # min-of-reps: the host VM time-dilates intermittently
SELS = [(1, "bucket < 1"), (10, "bucket < 10")]


def build():
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
    cur.execute("DROP TABLE IF EXISTS tv_items CASCADE")
    cur.execute("SELECT setseed(0.42)")
    cur.execute("CREATE TABLE tv_items (id serial PRIMARY KEY, bucket int, "
                "embedding vector(%s))" % DIM)
    t0 = time.perf_counter()
    cur.execute(
        "INSERT INTO tv_items (bucket, embedding) "
        "SELECT i %% 100, v.vec FROM generate_series(1, %s) i, LATERAL ("
        "  SELECT ('[' || string_agg(random()::text, ',') || ']')::vector AS vec"
        "  FROM generate_series(1, %s) d WHERE i = i) v" % (N, DIM))
    print(f"data_s={time.perf_counter() - t0:.1f}", flush=True)

    cur.execute("SET pg_acorn.enable_hook = off")
    cur.execute("SET pg_acorn.build_seed = 42")
    cur.execute("SET maintenance_work_mem = '2GB'")
    cur.execute("SET max_parallel_maintenance_workers = 4")
    cur.execute("ALTER TABLE tv_items SET (parallel_workers = 4)")
    for name, inline in (("tv_acorn_noinline", "false"), ("tv_acorn_idx", "true")):
        t0 = time.perf_counter()
        cur.execute(
            f"CREATE INDEX {name} ON tv_items "
            "USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops) "
            "WITH (m=16, ef_construction=64, acorn_gamma=2, "
            f"acorn_payload_edges=true, acorn_inline_vectors={inline})")
        cur.execute(f"SELECT pg_relation_size('{name}')")
        sz = cur.fetchone()[0]
        print(f"{name}: build_s={time.perf_counter() - t0:.1f} "
              f"size_mb={sz / 1048576:.0f}", flush=True)
    # fixed query vector, deterministic
    cur.execute("SELECT setseed(0.7)")
    cur.execute("SELECT ('[' || string_agg(random()::text, ',') || ']') "
                "FROM generate_series(1, %s)" % DIM)
    qv = cur.fetchone()[0]
    with open("/tmp/qv.json", "w") as f:
        json.dump(qv, f)
    conn.close()


def run_mode(mode, results):
    """mode: inline | cache | off"""
    with open("/tmp/qv.json") as f:
        qv = f.read().strip('"')
    conn = psycopg.connect(DSN, prepare_threshold=0)
    cur = conn.cursor()
    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET enable_sort = off")
    cur.execute("SET pg_acorn.member_first = on")
    cur.execute("SET pg_acorn.scan_inline_vectors = on")
    cur.execute("SET pg_acorn.scan_code_cache = " +
                ("on" if mode == "cache" else "off"))
    # psycopg auto-begins the transaction; DROP INDEX is rolled back at end
    drop = "tv_acorn_noinline" if mode == "inline" else "tv_acorn_idx"
    cur.execute(f"DROP INDEX {drop}")
    for sel, pred in SELS:
        for ef in EFS:
            cur.execute(f"SET pg_acorn.ef_search = {ef}")
            lat = []
            bufs = None
            for r in range(REPS + 1):
                t0 = time.perf_counter()
                cur.execute(
                    f"SELECT id FROM tv_items WHERE {pred} "
                    "ORDER BY embedding <=> %s::vector LIMIT 10", (qv,))
                rows = cur.fetchall()
                dt = (time.perf_counter() - t0) * 1000
                if r > 0:          # skip warm-up rep
                    lat.append(dt)
            cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, FORMAT JSON) "
                f"SELECT id FROM tv_items WHERE {pred} "
                "ORDER BY embedding <=> %s::vector LIMIT 10", (qv,))
            plan = cur.fetchone()[0][0]
            bufs = plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
            med = statistics.median(lat)
            mn = min(lat)
            key = (mode, sel, ef)
            results[key] = (mn, med, bufs, len(rows))
            print(f"{mode} sel={sel}% ef={ef}: min={mn:.2f}ms med={med:.2f}ms "
                  f"bufs={bufs} rows={len(rows)}", flush=True)
    conn.rollback()
    conn.close()


def sweep():
    results = {}
    for mode in sys.argv[2:] or ["off", "inline", "cache"]:
        run_mode(mode, results)
    print("\n=== summary (min ms / bufs; min-of-reps defeats host dilation) ===")
    for sel, _ in SELS:
        for ef in EFS:
            row = f"sel={sel}% ef={ef}:"
            for mode in ("off", "inline", "cache"):
                v = results.get((mode, sel, ef))
                row += f"  {mode}={v[0]:.2f}ms/{v[2]}b" if v else f"  {mode}=-"
            v_i = results.get(("inline", sel, ef))
            v_c = results.get(("cache", sel, ef))
            if v_i and v_c:
                row += f"  cache/inline={v_c[0] / v_i[0]:.2f}x"
            print(row)


if __name__ == "__main__":
    {"build": build, "sweep": sweep}[sys.argv[1]]()
