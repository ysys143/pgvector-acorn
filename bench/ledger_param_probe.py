"""Isolate the 13.3 ms vs 2.5 ms discrepancy between the prior benchmark's
query path (psycopg unprepared, q.tolist() list parameter) and the ledger's
path (pre-formatted '[...]' string, 6 decimals).

Variants (same table, same plan: Limit -> Sort -> BitmapHeapScan):
  A  list param, unprepared      (exact prior path)
  B  list param, prepared
  C  string param %.17g, unprepared  (same byte size as A, but text)
  D  string param %.6f, unprepared   (ledger path)
  E  server-side time of the list-param query via EXPLAIN ANALYZE
  F  jit=off variant of A (rule JIT in/out)

Prints mean/median/p99 per variant.
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import statistics
import time
import numpy as np
import psycopg

DSN = "postgresql://postgres:postgres@postgres/bench"
N, DIM, NQ, K, TH = 30_000, 128, 50, 10, 1
SEED = 42


def make_data():
    rng = np.random.default_rng(SEED)
    raw = rng.standard_normal((N, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    blocks = DIM // 10
    bn = np.array(
        [np.linalg.norm(vecs[:, i * 10 : (i + 1) * 10], axis=1) for i in range(blocks)]
    ).T
    buckets = np.clip((np.argmax(bn, axis=1) * (100 // blocks)).astype(int), 0, 99)
    return vecs, buckets


VECS, BUCKETS = make_data()
SQL = (
    "SELECT id FROM probe_items WHERE bucket < %s "
    "ORDER BY embedding <=> %s::vector LIMIT %s"
)

conn = psycopg.connect(DSN, autocommit=True)
with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("DROP TABLE IF EXISTS probe_items CASCADE")
    cur.execute(
        f"CREATE TABLE probe_items "
        f"(id serial PRIMARY KEY, bucket int, embedding vector({DIM}))"
    )
    with cur.copy("COPY probe_items (bucket, embedding) FROM STDIN") as cp:
        for i in range(N):
            cp.write_row(
                (int(BUCKETS[i]), "[" + ",".join(f"{x:.6f}" for x in VECS[i]) + "]")
            )
    cur.execute("CREATE INDEX ON probe_items (bucket)")
    cur.execute("VACUUM ANALYZE probe_items")
    cur.execute("SELECT count(*) FROM probe_items")


def run(label, param_fn, prepare, jit=None, reps=2):
    lat = []
    with conn.cursor() as cur:
        if jit is not None:
            cur.execute(f"SET jit = {jit}")
        for q in VECS[:NQ]:
            p = param_fn(q)
            for _ in range(reps):
                t0 = time.perf_counter()
                cur.execute(SQL, (TH, p, K), prepare=prepare)
                cur.fetchall()
                lat.append((time.perf_counter() - t0) * 1e3)
        if jit is not None:
            cur.execute("RESET jit")
    print(
        f"  {label:<38s} mean={np.mean(lat):7.2f}  med={statistics.median(lat):7.2f}  "
        f"p99={np.percentile(lat, 99):7.2f} ms",
        flush=True,
    )
    return statistics.median(lat)


def as_list(q):
    return q.tolist()


def as_s17(q):
    return "[" + ",".join(f"{x:.17g}" for x in q.tolist()) + "]"


def as_s6(q):
    return "[" + ",".join(f"{x:.6f}" for x in q) + "]"


print(f"variants over {NQ} queries x2, threshold={TH}:")
run("A list param, unprepared (prior path)", as_list, False)
run("B list param, prepared", as_list, True)
run("C string %.17g, unprepared", as_s17, False)
run("D string %.6f, unprepared (ledger)", as_s6, False)
run("F list param, unprepared, jit=off", as_list, False, jit="off")

# E: where does the time go server-side for the list-param query?
with conn.cursor() as cur:
    q = VECS[0]
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SQL, (TH, q.tolist(), K))
    plan = cur.fetchone()[0]
    print(
        f"  E explain(list param): exec={plan[0]['Execution Time']:.2f} ms "
        f"planning={plan[0].get('Planning Time', 0):.2f} ms"
    )

with conn.cursor() as cur:
    cur.execute("DROP TABLE IF EXISTS probe_items CASCADE")
conn.close()
