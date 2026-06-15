"""Download Cohere wikipedia embeddings (real data) and prepare the scale fixture.

Streams the first N+NQ rows of Cohere/wikipedia-22-12-en-embeddings (768-dim,
~35M public rows), saves the embeddings as a memmap-friendly .npy, assigns a
CORRELATED integer bucket per row (derived from the dominant embedding block,
mirroring thesis_validation so `bucket < sel` is the hard correlated filter), and
holds out NQ rows as queries.

Outputs (in --dir):
  emb.npy      (N x DIM float32)   indexed vectors
  bucket.npy   (N int16)           correlated filter value 0..99
  queries.npy  (NQ x DIM float32)  held-out query vectors

Run on the VM:
  ~/.local/bin/uv run --with numpy --with "datasets>=2" python3 bench/cohere_prep.py \
    --n 10000000 --dir ~/scale_data
"""
import argparse
import os
import time

import numpy as np

DATASET = "Cohere/wikipedia-2023-11-embed-multilingual-v3"
CONFIG = "en"
NQ = 40
NBLOCK = 10                       # 10 blocks -> bucket tens digit; bucket 0..99


def emb_of(row):
    v = row.get("emb")
    if v is None:
        v = row.get("embedding")
    return np.asarray(v, dtype=np.float32)


def assign_buckets(emb, rng):
    """Correlated bucket: dominant block (of NBLOCK) sets the tens digit."""
    n = emb.shape[0]
    bs = emb.shape[1] // NBLOCK
    block_norm = np.stack(
        [np.linalg.norm(emb[:, b * bs:(b + 1) * bs], axis=1) for b in range(NBLOCK)],
        axis=1)                                   # n x NBLOCK
    dominant = np.argmax(block_norm, axis=1)      # 0..NBLOCK-1
    spread = rng.integers(0, 100 // NBLOCK, size=n)
    return np.clip(dominant * (100 // NBLOCK) + spread, 0, 99).astype(np.int16)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000_000)
    ap.add_argument("--dir", default=os.path.expanduser("~/scale_data"))
    ap.add_argument("--dataset", default=DATASET)
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)

    from datasets import load_dataset
    total = args.n + NQ
    print(f"[stream] {args.dataset}:{args.config} first {total} rows ...", flush=True)
    ds = load_dataset(args.dataset, args.config, split="train", streaming=True)

    emb = None                      # allocated after first row (dim auto-detect)
    qrows = []
    i = 0
    t0 = time.time()
    for row in ds:
        v = emb_of(row)
        if emb is None:
            emb = np.empty((args.n, v.shape[0]), dtype=np.float32)
            print(f"  [dim] {v.shape[0]}", flush=True)
        if i < args.n:
            emb[i] = v
        else:
            qrows.append(v)
        i += 1
        if i % 500_000 == 0:
            print(f"  {i}/{total} ({(time.time()-t0):.0f}s)", flush=True)
        if i >= total:
            break
    if i < total:
        raise SystemExit(f"dataset exhausted at {i} < {total} (lower --n)")

    rng = np.random.default_rng(0)
    bucket = assign_buckets(emb, rng)
    np.save(os.path.join(args.dir, "emb.npy"), emb)
    np.save(os.path.join(args.dir, "bucket.npy"), bucket)
    np.save(os.path.join(args.dir, "queries.npy"), np.stack(qrows[:NQ]))
    # bucket distribution sanity (correlated -> not perfectly uniform)
    hist = np.bincount(bucket, minlength=100)
    print(f"[done] N={args.n} dim={emb.shape[1]} -> {args.dir}", flush=True)
    print(f"  bucket<1%={ (bucket<1).mean()*100:.2f}%  <5={ (bucket<5).mean()*100:.2f}%"
          f"  <10={ (bucket<10).mean()*100:.2f}%  <20={ (bucket<20).mean()*100:.2f}%",
          flush=True)
    print(f"  bucket hist[0:12]={hist[:12].tolist()}", flush=True)


if __name__ == "__main__":
    main()
