"""SIFT1M fixture: real SIFT vectors when available, else synthetic fallback.

Real benchmarks use SIFT1M base vectors from http://corpus-texmex.irisa.fr/
(sift_base.fvecs, 1,000,000 x 128-dim).  Set the SIFT_BASE environment variable
to the path of sift_base.fvecs to load real data; otherwise a synthetic
unit-vector fallback is generated (sufficient for relative recall, but lacks
real cluster structure — see docs/sigmod2026-fvs-postgresql-analysis.md).

Bucket metadata is uniform random in [0,99] so `bucket < N` gives ~N%
selectivity, independent of the vector (no filter/vector correlation; scenario
D covers correlated cases).
"""

from __future__ import annotations

import os
import numpy as np


def read_fvecs(path: str, max_n: int | None = None) -> np.ndarray:
    """Read a .fvecs file (repeated [int32 dim][dim float32]) into an (n, dim)
    float32 array.  Reads in one pass and slices to max_n."""
    raw = np.fromfile(path, dtype=np.int32)
    if raw.size == 0:
        return np.empty((0, 0), dtype=np.float32)
    dim = int(raw[0])
    raw = raw.reshape(-1, dim + 1)
    if max_n is not None:
        raw = raw[:max_n]
    return raw[:, 1:].copy().view(np.float32)


def load(n: int = 50_000, dim: int = 96, seed: int = 42) -> tuple[np.ndarray, list[dict]]:
    rng = np.random.default_rng(seed)

    sift_base = os.environ.get("SIFT_BASE")
    if sift_base and os.path.exists(sift_base):
        vectors = read_fvecs(sift_base, max_n=n)
    else:
        # Synthetic fallback: random unit vectors of the requested dimension.
        raw = rng.standard_normal((n, dim)).astype(np.float32)
        vectors = raw / np.linalg.norm(raw, axis=1, keepdims=True)

    buckets = rng.integers(0, 100, size=len(vectors))
    metadata = [{"bucket": int(b)} for b in buckets]
    return vectors, metadata
