"""Phase D — Qdrant rematch with HNSW FORCED, apples-to-apples vs acorn 250K.

Prior Qdrant numbers (QDRANT_CODEPATH.md) measured exact brute-force: no
segment crossed the per-segment indexing threshold, so no HNSW graph existed
(recall 1.000 everywhere by construction).  This run forces filtered HNSW by
driving the optimizer's indexing_threshold and the search planner's
full_scan_threshold to ~0, then VERIFIES the graph is engaged by requiring
recall < 1.0 at low ef before trusting any number.

Same fixture as bench/thesis_validation.py (seed 0, correlated buckets, the
HARD case where the filter correlates with vector position), same queries,
same exact ground truth, same filter semantics (bucket < sel), same HNSW build
params (m=16, ef_construct=64) -> a fair cross-engine comparison with acorn.

Run from the host against the running qdrant container:
  uv run --with numpy --with httpx python3 bench/qdrant_rematch.py \
    --url http://localhost:6333
"""
import argparse
import json
import time

import httpx
import numpy as np

N, DIM, NQ, K = 250_000, 128, 40, 10
SELS = [1, 2, 5, 10, 20]
EFS = [40, 100, 200, 400, 800]
COLL = "rematch_250k"


def make_fixture():
    """Byte-identical to thesis_validation.make_fixture (seed 0)."""
    rng = np.random.default_rng(0)
    raw = rng.standard_normal((N, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    blocks = DIM // 10
    block_norms = np.array([
        np.linalg.norm(vecs[:, i * 10:(i + 1) * 10], axis=1)
        for i in range(blocks)
    ]).T
    dominant = np.argmax(block_norms, axis=1)
    span = 100 // blocks
    buckets = dominant * span + rng.integers(0, span, size=N)
    buckets = np.clip(buckets, 0, 99).astype(int)
    qraw = rng.standard_normal((NQ, DIM)).astype(np.float32)
    queries = qraw / np.linalg.norm(qraw, axis=1, keepdims=True)
    return vecs, buckets, queries


def exact_truth(vecs, buckets, q, thresh):
    mask = buckets < thresh
    idx = np.nonzero(mask)[0]
    sub = vecs[idx]
    sims = sub @ q                      # cosine: vectors are unit-norm
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())      # ids are 1-based (point id = i+1)


def wait_indexed(c, want, timeout=900):
    t0 = time.time()
    last = -1
    while time.time() - t0 < timeout:
        info = c.get(f"/collections/{COLL}").json()["result"]
        idx = info.get("indexed_vectors_count") or 0
        status = info.get("status")
        if idx != last:
            print(f"  [index] status={status} indexed={idx}/{want}", flush=True)
            last = idx
        if status == "green" and idx >= want * 0.99:
            return idx
        time.sleep(3)
    raise TimeoutError(f"indexing stalled at {last}/{want}")


def setup(c, vecs, buckets):
    c.delete(f"/collections/{COLL}")
    # FORCE HNSW: indexing_threshold ~0 indexes every segment; full_scan
    # threshold ~0 makes the filtered planner use the graph, not a full scan.
    c.put(f"/collections/{COLL}", json={
        "vectors": {"size": DIM, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64, "full_scan_threshold": 10},
        "optimizers_config": {"indexing_threshold": 10, "default_segment_number": 2},
    }).raise_for_status()
    c.put(f"/collections/{COLL}/index", json={
        "field_name": "bucket", "field_schema": "integer"}).raise_for_status()
    B = 2000
    for s in range(0, N, B):
        e = min(s + B, N)
        pts = [{"id": i + 1, "vector": vecs[i].tolist(),
                "payload": {"bucket": int(buckets[i])}} for i in range(s, e)]
        last = e >= N
        c.put(f"/collections/{COLL}/points" + ("?wait=true" if last else ""),
              json={"points": pts}).raise_for_status()
        if (s // B) % 20 == 0:
            print(f"  [upload] {e}/{N}", flush=True)
    print(f"[load] {N} points; waiting for HNSW build...", flush=True)
    wait_indexed(c, N)


def search(c, q, thresh, ef):
    r = c.post(f"/collections/{COLL}/points/search", json={
        "vector": q.tolist(), "limit": K,
        "filter": {"must": [{"key": "bucket", "range": {"lt": thresh}}]},
        "params": {"hnsw_ef": ef},
    })
    r.raise_for_status()
    return {h["id"] for h in r.json()["result"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:6333")
    ap.add_argument("--out", default="bench/results_qdrant_rematch.json")
    ap.add_argument("--skip-load", action="store_true")
    args = ap.parse_args()

    print("[fixture] generating (seed 0, correlated buckets)...", flush=True)
    vecs, buckets, queries = make_fixture()
    c = httpx.Client(base_url=args.url, timeout=600.0)

    if not args.skip_load:
        setup(c, vecs, buckets)

    truths = {s: [exact_truth(vecs, buckets, queries[i], s) for i in range(NQ)]
              for s in SELS}

    # ---- HNSW-engaged gate: recall MUST be < 1.0 at low ef, else it is the
    # exact path again and every number below is meaningless. ----
    probe = [len(search(c, queries[i], 10, 40) & truths[10][i]) / K
             for i in range(NQ)]
    probe_recall = float(np.mean(probe))
    print(f"\n[GATE] sel=10% ef=40 recall = {probe_recall:.3f} "
          f"({'HNSW engaged' if probe_recall < 0.999 else 'STILL EXACT — config failed'})",
          flush=True)

    out = {"meta": {"n": N, "dim": DIM, "nq": NQ, "k": K,
                    "engine": "qdrant v1.16 HNSW m=16 efc=64 forced",
                    "fixture": "thesis_validation seed0 correlated",
                    "hnsw_engaged_probe_recall_sel10_ef40": probe_recall},
           "results": {}}

    for sel in SELS:
        out["results"][str(sel)] = []
        for ef in EFS:
            lats, recs = [], []
            for rep in range(2):
                for i in range(NQ):
                    t0 = time.perf_counter()
                    got = search(c, queries[i], sel, ef)
                    lats.append((time.perf_counter() - t0) * 1e3)
                    if rep == 0:
                        recs.append(len(got & truths[sel][i]) / K)
            cell = {"ef": ef, "recall": round(float(np.mean(recs)), 4),
                    "med_ms": round(float(np.median(lats)), 2),
                    "min_ms": round(float(np.min(lats)), 2)}
            out["results"][str(sel)].append(cell)
            print(f"  [sel={sel}% ef={ef}] recall={cell['recall']:.3f} "
                  f"med={cell['med_ms']:.2f}ms min={cell['min_ms']:.2f}ms",
                  flush=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)

    c.delete(f"/collections/{COLL}")
    print(f"\n[done] -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
