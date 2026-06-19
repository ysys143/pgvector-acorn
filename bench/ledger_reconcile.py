"""Reconciliation probe: why did the prior benchmark report 79-124 QPS for the
bitmap-prefilter KNN at threshold=1 when the ledger measures ~2.5 ms/query
(~400 QPS) for the same plan on the same data?

Replays the EXACT prior measurement path (run_bench targets: psycopg unprepared
query with a full-precision Python-list parameter, per-query wall clock, 100
queries) against the same 30K/dim=128 high-correlation table, on the now-idle
stack.  If this reproduces ~400 QPS, the prior absolute QPS numbers were
deflated by concurrent load in the shared Docker VM, not by anything in the
query path.
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import time
import numpy as np
import psycopg

DSN = "postgresql://postgres:postgres@postgres/bench"
N, DIM, NQ, K, TH = 30_000, 128, 100, 10, 1
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
conn = psycopg.connect(DSN, autocommit=True)
with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("DROP TABLE IF EXISTS reconcile_items CASCADE")
    cur.execute(
        f"CREATE TABLE reconcile_items "
        f"(id serial PRIMARY KEY, bucket int, embedding vector({DIM}))"
    )
    with cur.copy("COPY reconcile_items (bucket, embedding) FROM STDIN") as cp:
        for i in range(N):
            cp.write_row(
                (int(BUCKETS[i]), "[" + ",".join(f"{x:.6f}" for x in VECS[i]) + "]")
            )
    cur.execute("CREATE INDEX ON reconcile_items (bucket)")
    cur.execute("VACUUM ANALYZE reconcile_items")
    cur.execute("SELECT count(*) FROM reconcile_items")  # warm cache
    cur.execute(
        "SELECT sum(embedding <=> (SELECT embedding FROM reconcile_items LIMIT 1)) FROM reconcile_items"
    )

# exact prior path: targets/pg_acorn.py query_filtered (unprepared, tolist param)
lat = []
with conn.cursor() as cur:
    for q in VECS[:NQ]:
        t0 = time.perf_counter()
        cur.execute(
            "SELECT id FROM reconcile_items WHERE bucket < %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (TH, q.tolist(), K),
        )
        cur.fetchall()
        lat.append(time.perf_counter() - t0)
qps = NQ / sum(lat)
print(
    f"prior-style (unprepared, tolist param): "
    f"mean={np.mean(lat) * 1e3:.2f} ms  p99={np.percentile(lat, 99) * 1e3:.2f} ms  "
    f"QPS={qps:.1f}"
)
print(
    "prior benchmark reported: 79.0 QPS (prefilter_exact) / "
    "89-124 QPS (bitmap points) at the same threshold"
)
with conn.cursor() as cur:
    cur.execute(
        "EXPLAIN (FORMAT JSON) SELECT id FROM reconcile_items "
        "WHERE bucket < 1 ORDER BY embedding <=> %s::vector LIMIT 10",
        (VECS[0].tolist(),),
    )
    plan = cur.fetchone()[0]

    def walk(n, acc):
        acc.append(n["Node Type"])
        for c in n.get("Plans", []):
            walk(c, acc)
        return acc

    print("plan:", " -> ".join(walk(plan[0]["Plan"], [])))
    cur.execute("DROP TABLE IF EXISTS reconcile_items CASCADE")
conn.close()
