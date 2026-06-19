"""Tail-spike attribution probe.

ledger_param_probe showed every PG query-path variant has median ~2-3 ms but
p99 of 80-190 ms; the prior benchmark's QPS = N/sum(latencies) is governed by
that tail.  This probe decides where the tail lives:

  - PG loop: EXPLAIN ANALYZE per call -> (client_ms, server_exec_ms) pairs.
      tail in client but not exec  -> driver / VM / protocol stall
      tail in exec too             -> server-side stall
  - Qdrant loop on the same data   -> does the HTTP path show the same tail?

Spike threshold: 5x the run median.
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import time
import numpy as np
import psycopg
import httpx

DSN = "postgresql://postgres:postgres@postgres/bench"
QDRANT = "http://qdrant:6333"
N, DIM, K, TH = 30_000, 128, 10, 1
SEED = 42
ITERS = 300


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
    cur.execute("DROP TABLE IF EXISTS tail_items CASCADE")
    cur.execute(
        f"CREATE TABLE tail_items "
        f"(id serial PRIMARY KEY, bucket int, embedding vector({DIM}))"
    )
    with cur.copy("COPY tail_items (bucket, embedding) FROM STDIN") as cp:
        for i in range(N):
            cp.write_row(
                (int(BUCKETS[i]), "[" + ",".join(f"{x:.6f}" for x in VECS[i]) + "]")
            )
    cur.execute("CREATE INDEX ON tail_items (bucket)")
    cur.execute("VACUUM ANALYZE tail_items")
    cur.execute("SELECT count(*) FROM tail_items")

SQL = (
    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
    "SELECT id FROM tail_items WHERE bucket < %s "
    "ORDER BY embedding <=> %s::vector LIMIT %s"
)

pairs = []
with conn.cursor() as cur:
    for i in range(ITERS):
        q = VECS[i % 50]
        t0 = time.perf_counter()
        cur.execute(SQL, (TH, q.tolist(), K))
        plan = cur.fetchone()[0]
        client = (time.perf_counter() - t0) * 1e3
        pairs.append((client, plan[0]["Execution Time"]))

cl = np.array([p[0] for p in pairs])
ex = np.array([p[1] for p in pairs])
med_cl, med_ex = float(np.median(cl)), float(np.median(ex))
spike_cl = cl > 5 * med_cl
spike_ex = ex > 5 * med_ex
print(
    f"PG  client: med={med_cl:6.2f} mean={cl.mean():6.2f} p99={np.percentile(cl, 99):7.2f} "
    f"spikes(>5x med)={spike_cl.sum()}/{ITERS}"
)
print(
    f"PG  exec  : med={med_ex:6.2f} mean={ex.mean():6.2f} p99={np.percentile(ex, 99):7.2f} "
    f"spikes(>5x med)={spike_ex.sum()}/{ITERS}"
)
print(
    f"  of {spike_cl.sum()} client spikes, {int((spike_cl & spike_ex).sum())} "
    f"also spike in server exec time"
)
if spike_cl.any():
    worst = np.argsort(-cl)[:5]
    for i in worst:
        print(f"    worst: client={cl[i]:8.2f} ms  exec={ex[i]:8.2f} ms")

# Qdrant on the same data
c = httpx.Client(base_url=QDRANT, timeout=300.0)
c.delete("/collections/tail_items")
c.put(
    "/collections/tail_items",
    json={
        "vectors": {"size": DIM, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64},
    },
).raise_for_status()
c.put(
    "/collections/tail_items/index",
    json={"field_name": "bucket", "field_schema": "integer"},
).raise_for_status()
for s0 in range(0, N, 2000):
    e0 = min(s0 + 2000, N)
    pts = [
        {
            "id": i + 1,
            "vector": VECS[i].tolist(),
            "payload": {"bucket": int(BUCKETS[i])},
        }
        for i in range(s0, e0)
    ]
    c.put(
        "/collections/tail_items/points" + ("?wait=true" if e0 >= N else ""),
        json={"points": pts},
    ).raise_for_status()
for _ in range(120):
    if c.get("/collections/tail_items").json()["result"]["status"] == "green":
        break
    time.sleep(1)

ql = []
for i in range(ITERS):
    q = VECS[i % 50]
    body = {
        "vector": q.tolist(),
        "limit": K,
        "filter": {"must": [{"key": "bucket", "range": {"lt": TH}}]},
        "params": {"hnsw_ef": 128},
    }
    t0 = time.perf_counter()
    c.post("/collections/tail_items/points/search", json=body).raise_for_status()
    ql.append((time.perf_counter() - t0) * 1e3)
ql = np.array(ql)
medq = float(np.median(ql))
print(
    f"QDR client: med={medq:6.2f} mean={ql.mean():6.2f} p99={np.percentile(ql, 99):7.2f} "
    f"spikes(>5x med)={(ql > 5 * medq).sum()}/{ITERS}"
)
print(
    f"implied QPS (N/sum): PG={ITERS / cl.sum() * 1e3:.1f}  QDR={ITERS / ql.sum() * 1e3:.1f}  "
    f"| median-based: PG={1e3 / med_cl:.1f}  QDR={1e3 / medq:.1f}"
)

c.delete("/collections/tail_items")
c.close()
with conn.cursor() as cur:
    cur.execute("DROP TABLE IF EXISTS tail_items CASCADE")
conn.close()
