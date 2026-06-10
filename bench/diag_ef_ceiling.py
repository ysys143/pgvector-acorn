"""Diagnostic: is pg_acorn tier2's recall ceiling a SEARCH gap or a BUILD gap?

On a small adversarial set (n=20000, 1% selectivity => ~200 passing points),
sweep pg_acorn.ef_search up to the GUC max (4000 ~= near-full traversal) and
measure recall@10 vs exact (numpy) ground truth.

  - If recall -> 1.0 as ef grows: all passing nodes ARE reachable; the gap to
    Qdrant is search EFFICIENCY (Qdrant finds them with less work). Tractable.
  - If recall PLATEAUS below 1.0 even near-full traversal: passing nodes are
    unreachable in the built graph -> a BUILD/connectivity gap (needs 2-hop or
    payload-aware edges, the mechanism Qdrant uses). Deeper.

Uses its own table (diag_items) so it can run alongside the main benchmark.
"""

import time
import numpy as np
import psycopg

DSN = "postgresql://postgres:postgres@postgres/bench"
N, DIM, NQ, K = 20000, 128, 30, 10
GAMMA = 2
EFS = [200, 400, 800, 1600, 2400, 4000]

rng = np.random.default_rng(0)
raw = rng.standard_normal((N, DIM)).astype(np.float32)
vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
buckets = rng.integers(0, 100, size=N)          # adversarial: bucket ⟂ vector
queries = vecs[:NQ]


def exact_truth(q, thresh, k):
    idx = np.where(buckets < thresh)[0]
    sims = vecs[idx] @ q                          # cosine (unit vectors)
    top = idx[np.argsort(-sims)[:k]]
    return set((top + 1).tolist())                # ids are i+1 (serial from 1)


conn = psycopg.connect(DSN, autocommit=True)
with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
    cur.execute("DROP TABLE IF EXISTS diag_items CASCADE")
    cur.execute(f"CREATE TABLE diag_items (id serial PRIMARY KEY, bucket int, embedding vector({DIM}))")
    print(f"loading {N} rows...", flush=True)
    with cur.copy("COPY diag_items (bucket, embedding) FROM STDIN") as cp:
        for i in range(N):
            cp.write_row((int(buckets[i]), "[" + ",".join(f"{x:.6f}" for x in vecs[i]) + "]"))
    print(f"building acorn_hnsw gamma={GAMMA}...", flush=True)
    cur.execute(f"""CREATE INDEX ON diag_items
                    USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops)
                    WITH (m = 16, ef_construction = 64, acorn_gamma = {GAMMA})""")

SEL = 1   # bucket < 1 => bucket 0 => ~1% => ~200 passing
qstr = lambda q: "[" + ",".join(f"{x:.6f}" for x in q) + "]"

print(f"\n=== ef_search ceiling sweep, sel={SEL}% (~{int(SEL/100*N)} passing), gamma={GAMMA} ===", flush=True)
for ef in EFS:
    recalls, t0 = [], time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(f"SET pg_acorn.ef_search = {ef}")
        for q in queries:
            truth = exact_truth(q, SEL, K)
            cur.execute(
                "SELECT id FROM diag_items WHERE bucket < %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (SEL, qstr(q), K),
            )
            ids = {r[0] for r in cur.fetchall()}
            recalls.append(len(ids & truth) / K)
    qps = NQ / (time.perf_counter() - t0)
    print(f"  ef={ef:>5}  recall={np.mean(recalls):.3f}  qps={qps:.1f}", flush=True)

with conn.cursor() as cur:
    cur.execute("DROP TABLE IF EXISTS diag_items CASCADE")
print("done", flush=True)
