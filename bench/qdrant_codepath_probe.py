"""Probe: which Qdrant code path did our benchmark numbers actually measure?

Hypothesis (from reading qdrant v1.16.0 source): at n=30K/50K our collections
never crossed the per-segment indexing threshold, so no segment ever got an
HNSW graph and every search -- filtered or not, any hnsw_ef -- was an exact
brute-force scan through PlainVectorIndex (recall 1.0 by construction).

Mechanics being verified (lib paths in qdrant v1.16.0):
  - optimizers_builder.rs:21  DEFAULT_INDEXING_THRESHOLD_KB = 10_000 (KB of
    vector storage PER SEGMENT, 512 B/vector at dim=128 f32).
  - optimizers_builder.rs:104-113  default_segment_number =
    clamp(num_cpus/2, 2, 8); our container sees 8 CPUs -> 4 segments ->
    30K pts = 3_750 KiB/segment, 50K = 6_250 KiB/segment: never indexed.
  - segment_optimizer.rs:239-260  only segments >= threshold get
    Indexes::Hnsw; below it they keep Indexes::Plain.
  - plain_vector_index.rs:84-140  plain filtered search scores ALL filtered
    ids exactly; indexed_vector_count() == 0 (line 222).
  - hnsw.rs:244-253  full_scan_threshold KB -> points:
    10_000 KB * 1024 / 512 B = 20_000 points. Even on an indexed segment,
    a filter whose per-segment cardinality estimate stays below 20_000
    points is answered by an exact scan (hnsw.rs:1395-1406).

Phases:
  A  n=30K, exact bench config (defaults). Expect indexed_vectors_count == 0,
     recall 1.0 everywhere, latency flat in hnsw_ef, telemetry counts go to
     'filtered_plain' (= PlainVectorIndex, no HNSW on the segment at all).
  B  n=60K, default_segment_number=2 (-> 15_000 KiB/segment, crosses the
     10_000 KiB threshold; with the default 4 segments even 60K would NOT
     cross). Expect indexed_vectors_count -> ~60K. Filtered searches at
     sel=1%/40% should STILL be exact (per-segment cardinality 300/12_000
     < 20_000 -> 'filtered_small_cardinality' plain path); sel=80%
     (24_000/segment) and unfiltered should hit the graph and become
     ef-dependent.
  C  n=60K indexed, full_scan_threshold=10 (KB -> 10*1024/512 = 20 points):
     forces graph traversal for any real filter. Measures genuine filtered
     HNSW recall/latency vs hnsw_ef at sel=1% and 40%.

Run:
  docker compose -f docker/docker-compose.yml --profile bench run --rm \
      --no-deps bench python3 -u bench/qdrant_codepath_probe.py
"""

import statistics
import time

import httpx
import numpy as np

URL = "http://qdrant:6333"
DIM, NQ, K = 128, 30, 10
UPSERT_BATCH = 2000
EF_SWEEP = [10, 50, 100, 200, 400]

TELEMETRY_KEYS = (
    "unfiltered_plain",
    "filtered_plain",
    "unfiltered_hnsw",
    "filtered_small_cardinality",
    "filtered_large_cardinality",
    "filtered_exact",
    "unfiltered_exact",
)

client = httpx.Client(base_url=URL, timeout=600.0)


def make_data(n, seed=0):
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal((n, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    buckets = rng.integers(0, 100, size=n)  # adversarial: bucket independent of vector
    return vecs, buckets


def create_collection(name, hnsw_extra=None, optimizers=None):
    client.delete(f"/collections/{name}")
    time.sleep(1)  # delete is async
    body = {
        "vectors": {"size": DIM, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64, **(hnsw_extra or {})},
    }
    if optimizers:
        body["optimizers_config"] = optimizers
    client.put(f"/collections/{name}", json=body).raise_for_status()
    # payload index BEFORE upsert, same order as bench/targets/qdrant.py
    client.put(
        f"/collections/{name}/index",
        json={"field_name": "bucket", "field_schema": "integer"},
    ).raise_for_status()


def upsert(name, vecs, buckets):
    n = len(vecs)
    for s in range(0, n, UPSERT_BATCH):
        e = min(s + UPSERT_BATCH, n)
        pts = [
            {"id": i + 1, "vector": vecs[i].tolist(), "payload": {"bucket": int(buckets[i])}}
            for i in range(s, e)
        ]
        client.put(
            f"/collections/{name}/points" + ("?wait=true" if e >= n else ""),
            json={"points": pts},
        ).raise_for_status()


def info(name):
    r = client.get(f"/collections/{name}").json()["result"]
    return {
        "status": r["status"],
        "optimizer_status": r["optimizer_status"],
        "points_count": r["points_count"],
        "indexed_vectors_count": r["indexed_vectors_count"],
        "segments_count": r["segments_count"],
    }


def poll_until_settled(name, settle_s=20, timeout_s=420):
    """Poll GET /collections/{name}; print the indexed_vectors_count time series."""
    t0 = time.time()
    last_key, stable_since = None, time.time()
    while time.time() - t0 < timeout_s:
        i = info(name)
        key = (i["status"], i["indexed_vectors_count"])
        if key != last_key:
            print(
                f"    t={time.time() - t0:6.1f}s status={i['status']:<6} "
                f"points={i['points_count']} indexed_vectors={i['indexed_vectors_count']} "
                f"segments={i['segments_count']}",
                flush=True,
            )
            last_key, stable_since = key, time.time()
        if i["status"] == "green" and time.time() - stable_since >= settle_s:
            break
        time.sleep(2)
    final = info(name)
    print(f"    settled: {final}", flush=True)
    return final


def telemetry_counts():
    """Sum VectorIndexSearchesTelemetry counters across all segments."""
    r = client.get("/telemetry", params={"details_level": 10}).json()["result"]
    tot = dict.fromkeys(TELEMETRY_KEYS, 0)

    def walk(o):
        if isinstance(o, dict):
            # VectorIndexSearchesTelemetry entries carry "index_name" and skip
            # zero-count counters during serialization.
            if "index_name" in o:
                for k in TELEMETRY_KEYS:
                    v = o.get(k) or {}
                    if isinstance(v, dict):
                        tot[k] += int(v.get("count") or 0)
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(r)
    return tot


def exact_truth(vecs, buckets, q, thresh):
    idx = np.where(buckets < thresh)[0]
    sims = vecs[idx] @ q
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())  # qdrant ids are i+1


def measure(name, vecs, buckets, sel, label, params=None, unfiltered=False):
    """30 filtered searches; returns (mean recall, median ms, telemetry delta)."""
    queries = vecs[:NQ]
    pre = telemetry_counts()
    recalls, lats = [], []
    for q in queries:
        body = {"vector": q.tolist(), "limit": K}
        if not unfiltered:
            body["filter"] = {"must": [{"key": "bucket", "range": {"lt": sel}}]}
        if params:
            body["params"] = params
        t0 = time.perf_counter()
        resp = client.post(f"/collections/{name}/points/search", json=body)
        lats.append((time.perf_counter() - t0) * 1000)
        resp.raise_for_status()
        ids = {h["id"] for h in resp.json()["result"]}
        if unfiltered:
            sims = vecs @ q
            truth = set((np.argsort(-sims)[:K] + 1).tolist())
        else:
            truth = exact_truth(vecs, buckets, q, sel)
        recalls.append(len(ids & truth) / K)
    post = telemetry_counts()
    delta = {k: post[k] - pre[k] for k in TELEMETRY_KEYS if post[k] - pre[k]}
    rec, p50 = float(np.mean(recalls)), statistics.median(lats)
    print(f"    {label:<38} recall={rec:.3f}  p50={p50:6.2f}ms  paths={delta}", flush=True)
    return rec, p50, delta


def run_phase(name, n, seed, hnsw_extra, optimizers, plan):
    vecs, buckets = make_data(n, seed)
    print(f"  creating {name} (n={n}, hnsw_extra={hnsw_extra}, optimizers={optimizers})", flush=True)
    create_collection(name, hnsw_extra, optimizers)
    upsert(name, vecs, buckets)
    poll_until_settled(name)
    for sel, params, label, unfiltered in plan:
        measure(name, vecs, buckets, sel, label, params, unfiltered)
    return vecs, buckets


def main():
    names = ["probe_30k_bench", "probe_60k_indexed", "probe_60k_fst10"]

    print("=== Phase A: n=30K, exact bench config (all defaults) ===", flush=True)
    plan_a = []
    for sel in (1, 40):
        plan_a.append((sel, None, f"sel={sel}% default", False))
        plan_a.append((sel, {"exact": True}, f"sel={sel}% exact=true", False))
        for ef in (10, 100, 400):
            plan_a.append((sel, {"hnsw_ef": ef}, f"sel={sel}% hnsw_ef={ef}", False))
    run_phase(names[0], 30_000, 0, None, None, plan_a)

    print("\n=== Phase B: n=60K, default_segment_number=2 -> crosses 10MB/segment ===", flush=True)
    plan_b = []
    for sel in (1, 40, 80):
        plan_b.append((sel, None, f"sel={sel}% default", False))
        plan_b.append((sel, {"exact": True}, f"sel={sel}% exact=true", False))
        for ef in (10, 400):
            plan_b.append((sel, {"hnsw_ef": ef}, f"sel={sel}% hnsw_ef={ef}", False))
    for ef in (10, 400):
        plan_b.append((0, {"hnsw_ef": ef}, f"unfiltered hnsw_ef={ef}", True))
    run_phase(names[1], 60_000, 1, None, {"default_segment_number": 2}, plan_b)

    print("\n=== Phase C: n=60K indexed, full_scan_threshold=10 KB (=20 pts) -> forced graph ===", flush=True)
    plan_c = []
    for sel in (1, 40):
        for ef in EF_SWEEP:
            plan_c.append((sel, {"hnsw_ef": ef}, f"sel={sel}% hnsw_ef={ef}", False))
        plan_c.append((sel, {"exact": True}, f"sel={sel}% exact=true", False))
    run_phase(names[2], 60_000, 1, {"full_scan_threshold": 10}, {"default_segment_number": 2}, plan_c)

    print("\ncleaning up...", flush=True)
    for name in names:
        client.delete(f"/collections/{name}")
    print("done", flush=True)


if __name__ == "__main__":
    main()
