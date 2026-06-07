"""SIFT-like fixture: random unit vectors + categorical bucket metadata.

For real benchmarks, replace with actual SIFT1M vectors from:
    http://corpus-texmex.irisa.fr/

The synthetic fallback generates random unit vectors of the requested dimension,
which preserves statistical properties needed for meaningful recall measurement.
"""

from __future__ import annotations

import numpy as np


def load(n: int = 50_000, dim: int = 96, seed: int = 42) -> tuple[np.ndarray, list[dict]]:
    rng = np.random.default_rng(seed)

    # Random unit vectors (approximate SIFT distribution)
    raw = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    vectors = raw / norms

    # Bucket metadata: bucket in [0, 99], uniform distribution
    # bucket < N gives N% selectivity
    buckets = rng.integers(0, 100, size=n)
    metadata = [{"bucket": int(b)} for b in buckets]

    return vectors, metadata
