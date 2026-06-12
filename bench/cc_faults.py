#!/usr/bin/env python3
"""Per-query latency vs page-fault correlation for the cache-mode scan."""
import json
import sys
import time

import psycopg

DSN = "host=/var/run/postgresql dbname=postgres user=postgres"
MODE = sys.argv[1] if len(sys.argv) > 1 else "cache"
EF = int(sys.argv[2]) if len(sys.argv) > 2 else 1600
REPS = int(sys.argv[3]) if len(sys.argv) > 3 else 20

with open("/tmp/qv.json") as f:
    qv = json.load(f)

conn = psycopg.connect(DSN, prepare_threshold=0)
cur = conn.cursor()
cur.execute("SELECT pg_backend_pid()")
pid = cur.fetchone()[0]

def faults():
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().split()
    minflt, majflt = int(parts[9]), int(parts[11])
    utime, stime = int(parts[13]), int(parts[14])   # clock ticks (100/s)
    vcs = nvcs = 0
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("voluntary_ctxt_switches"):
                vcs = int(line.split()[1])
            elif line.startswith("nonvoluntary_ctxt_switches"):
                nvcs = int(line.split()[1])
    return minflt, majflt, utime + stime, vcs, nvcs

cur.execute("SET enable_seqscan = off")
cur.execute("SET enable_bitmapscan = off")
cur.execute("SET pg_acorn.member_first = on")
cur.execute("SET pg_acorn.scan_code_cache = " + ("on" if MODE == "cache" else "off"))
cur.execute(f"SET pg_acorn.ef_search = {EF}")
drop = "tv_acorn_noinline" if MODE == "inline" else "tv_acorn_idx"
cur.execute(f"DROP INDEX {drop}")

for r in range(REPS):
    mn0, mj0, cpu0, vc0, nv0 = faults()
    t0 = time.perf_counter()
    cur.execute(
        "SELECT count(*) FROM (SELECT id FROM tv_items WHERE bucket < 1 "
        "ORDER BY embedding <=> %s::vector LIMIT 10) s", (qv,))
    cur.fetchall()
    dt = (time.perf_counter() - t0) * 1000
    mn1, mj1, cpu1, vc1, nv1 = faults()
    print(f"rep={r:2d} t={dt:8.2f}ms cpu={(cpu1-cpu0)*10:6d}ms "
          f"minflt={mn1-mn0:6d} majflt={mj1-mj0:4d} "
          f"vcs={vc1-vc0:5d} nvcs={nv1-nv0:5d}", flush=True)
conn.rollback()
