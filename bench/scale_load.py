"""Load the first N rows of the prepared Cohere fixture into Postgres + Qdrant,
and compute exact truth for the query set. Runs on the VM.

Reads emb.npy / bucket.npy / queries.npy from --dir (dim inferred from emb).
  - Postgres: (re)create tv_items(id, bucket, embedding vector(D)) and COPY N rows.
  - Qdrant: (re)create collection 'cohere' and upsert N points (forced HNSW).
  - Truth: for each query x selectivity, top-10 by cosine among bucket<sel rows
    (vectors are unit-norm -> cosine = dot); saved to truth_<N>.npz.

  ~/.local/bin/uv run --with numpy --with "psycopg[binary]" --with httpx \
    python3 bench/scale_load.py --n 100000 --dir ~/scale_data \
    --pg "host=127.0.0.1 user=postgres password=postgres dbname=bench" \
    --qdrant http://localhost:6333
"""
import argparse
import json
import os
import time

import numpy as np


SELS = [1, 2, 5, 10, 20]
K = 10
QCOLL = "cohere"


def load_pg(dsn, emb, bucket, n):
    """Bulk-load via COPY ... FORMAT BINARY, building the pgvector wire bytes by
    hand (no pgvector/dumper dependency). The per-row float conversion is done
    one batch at a time with astype('>f4') (C-level) instead of per-element
    Python formatting -- the difference between minutes and hours at 10M x 1024."""
    import struct
    import psycopg
    dim = emb.shape[1]
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("DROP TABLE IF EXISTS tv_items")
    cur.execute(f"CREATE TABLE tv_items (id int PRIMARY KEY, bucket int, embedding vector({dim}))")
    HDR = b"PGCOPY\n\xff\r\n\x00" + struct.pack(">ii", 0, 0)   # signature + flags + ext-len
    TRL = struct.pack(">h", -1)
    NF = struct.pack(">h", 3)                                  # 3 fields per row
    VHDR = struct.pack(">HH", dim, 0)                          # vector field: dim + unused
    VLEN = struct.pack(">i", 4 + 4 * dim)                      # int32 length of the vector field
    BATCH = 10000
    t0 = time.time()
    with cur.copy("COPY tv_items (id, bucket, embedding) FROM STDIN WITH (FORMAT BINARY)") as cp:
        cp.write(HDR)
        for s in range(0, n, BATCH):
            e = min(s + BATCH, n)
            be = np.ascontiguousarray(emb[s:e]).astype(">f4")  # batch -> big-endian float4
            buf = bytearray()
            for j in range(e - s):
                i = s + j
                buf += NF
                buf += struct.pack(">ii", 4, i)               # id (len + int4)
                buf += struct.pack(">ii", 4, int(bucket[i]))  # bucket (len + int4)
                buf += VLEN + VHDR + be[j].tobytes()          # embedding
            cp.write(bytes(buf))
            if s and (s // BATCH) % 50 == 0:
                print(f"  [pg copy] {e}/{n} ({time.time()-t0:.0f}s)", flush=True)
        cp.write(TRL)
    cur.execute("CREATE INDEX tv_bucket_btree ON tv_items (bucket)")
    cur.execute("ANALYZE tv_items")
    print(f"[pg] loaded {n} rows in {time.time()-t0:.0f}s", flush=True)
    conn.close()


def load_qdrant(url, emb, bucket, n, dim):
    import httpx
    c = httpx.Client(base_url=url, timeout=600.0)
    c.delete(f"/collections/{QCOLL}")
    c.put(f"/collections/{QCOLL}", json={
        "vectors": {"size": dim, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64, "full_scan_threshold": 10},
        "optimizers_config": {"indexing_threshold": 10, "default_segment_number": 4},
    }).raise_for_status()
    c.put(f"/collections/{QCOLL}/index",
          json={"field_name": "bucket", "field_schema": "integer"}).raise_for_status()
    # Qdrant caps a request at 32 MB; at dim=1024 a JSON batch of ~1000 points is
    # ~20 MB (5000 would be ~102 MB -> 400). Keep batches comfortably under the cap.
    B = max(200, min(1000, 25_000_000 // (dim * 24)))
    t0 = time.time()
    for s in range(0, n, B):
        e = min(s + B, n)
        pts = [{"id": i, "vector": emb[i].tolist(), "payload": {"bucket": int(bucket[i])}}
               for i in range(s, e)]
        last = e >= n
        c.put(f"/collections/{QCOLL}/points" + ("?wait=true" if last else ""),
              json={"points": pts}).raise_for_status()
        if (s // B) % 40 == 0:
            print(f"  [qdrant] {e}/{n} ({time.time()-t0:.0f}s)", flush=True)
    print(f"[qdrant] uploaded {n} in {time.time()-t0:.0f}s; HNSW building in background", flush=True)
    c.close()


def compute_truth(emb, bucket, queries, n):
    """top-K ids per (query, sel) by cosine among bucket<sel; brute force."""
    sub = emb[:n]                                  # n x dim (unit norm)
    bk = bucket[:n]
    sims = sub @ queries.T                         # n x NQ
    truth = {}
    for sel in SELS:
        mask = bk < sel
        idx = np.nonzero(mask)[0]
        ssel = sims[idx]                           # |pass| x NQ
        tt = []
        for qi in range(queries.shape[0]):
            order = np.argsort(-ssel[:, qi])[:K]
            tt.append(idx[order].tolist())
        truth[str(sel)] = tt
        print(f"  [truth] sel={sel}% pass={mask.mean()*100:.2f}%", flush=True)
    return truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--dir", default=os.path.expanduser("~/scale_data"))
    ap.add_argument("--pg", default="host=127.0.0.1 user=postgres password=postgres dbname=bench")
    ap.add_argument("--qdrant", default="http://localhost:6333")
    ap.add_argument("--skip-pg", action="store_true")
    ap.add_argument("--skip-qdrant", action="store_true")
    args = ap.parse_args()

    emb = np.load(os.path.join(args.dir, "emb.npy"), mmap_mode="r")
    bucket = np.load(os.path.join(args.dir, "bucket.npy"))
    queries = np.load(os.path.join(args.dir, "queries.npy")).astype(np.float32)
    n = min(args.n, emb.shape[0])
    dim = emb.shape[1]
    print(f"[load] N={n} dim={dim}", flush=True)

    if not args.skip_pg:
        load_pg(args.pg, emb, bucket, n)
    if not args.skip_qdrant:
        load_qdrant(args.qdrant, emb, bucket, n, dim)

    print("[truth] computing exact ground truth ...", flush=True)
    truth = compute_truth(np.asarray(emb), bucket, queries, n)
    out = os.path.join(args.dir, f"truth_{n}.npz")
    np.savez(out, **{k: np.array(v) for k, v in truth.items()})
    with open(os.path.join(args.dir, f"truth_{n}.json"), "w") as f:
        json.dump({"n": n, "dim": dim, "sels": SELS, "k": K}, f)
    print(f"[done] truth -> {out}", flush=True)


if __name__ == "__main__":
    main()
