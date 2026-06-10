"""Controlled Qdrant experiment: is recall=1.0 at high selectivity just exact
fallback (full_scan_threshold), or genuinely good filterable-HNSW?

Builds the SAME adversarial data twice — once with full_scan_threshold=10000
(default: exact search when the filtered set is small) and once with
full_scan_threshold=1 (force HNSW even for tiny filtered sets). If recall at 1%
selectivity collapses when the threshold is forced to 1, Qdrant's high-
selectivity win was pure prefilter-exact, not superior filter-aware HNSW.
"""

import time
import numpy as np
import httpx

URL = "http://qdrant:6333"
COLL = "qtest_threshold"
N, DIM, NQ, K = 50000, 128, 30, 10

rng = np.random.default_rng(0)
raw = rng.standard_normal((N, DIM)).astype(np.float32)
vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
buckets = rng.integers(0, 100, size=N)           # adversarial: bucket ⟂ vector
queries = vecs[:NQ]

client = httpx.Client(base_url=URL, timeout=600.0)


def exact_truth(q, thresh, k):
    idx = np.where(buckets < thresh)[0]
    sims = vecs[idx] @ q                          # cosine (unit vectors)
    top = idx[np.argsort(-sims)[:k]]
    return set((top + 1).tolist())               # qdrant ids are i+1


def setup(full_scan_threshold):
    client.delete(f"/collections/{COLL}")
    client.put(f"/collections/{COLL}", json={
        "vectors": {"size": DIM, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64,
                        "full_scan_threshold": full_scan_threshold},
    }).raise_for_status()
    client.put(f"/collections/{COLL}/index",
               json={"field_name": "bucket", "field_schema": "integer"}).raise_for_status()
    pts = [{"id": i + 1, "vector": vecs[i].tolist(), "payload": {"bucket": int(buckets[i])}}
           for i in range(N)]
    for s in range(0, N, 2000):
        last = s + 2000 >= N
        client.put(f"/collections/{COLL}/points" + ("?wait=true" if last else ""),
                   json={"points": pts[s:s + 2000]}).raise_for_status()


def measure(sel_pct):
    thresh = sel_pct                              # buckets 0..99 → bucket<sel = sel%
    recalls, t0 = [], time.perf_counter()
    for q in queries:
        truth = exact_truth(q, thresh, K)
        resp = client.post(f"/collections/{COLL}/points/search", json={
            "vector": q.tolist(), "limit": K,
            "filter": {"must": [{"key": "bucket", "range": {"lt": thresh}}]},
        })
        ids = {h["id"] for h in resp.json()["result"]}
        recalls.append(len(ids & truth) / K)
    return float(np.mean(recalls)), NQ / (time.perf_counter() - t0)


for fst in (10000, 1):
    setup(fst)
    print(f"--- full_scan_threshold={fst} ---", flush=True)
    for sel in (1, 25):
        r, q = measure(sel)
        print(f"  sel={sel}% (~{int(sel/100*N)} pts pass): recall={r:.3f} qps={q:.1f}", flush=True)

client.delete(f"/collections/{COLL}")
print("done", flush=True)
