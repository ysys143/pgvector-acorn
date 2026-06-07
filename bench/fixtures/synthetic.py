"""Synthetic fixture with controllable filter-query correlation.

correlation='high': filter bucket is determined by the dominant vector dimension,
    so the filter predicate and vector space are spatially aligned.
correlation='low':  bucket is assigned randomly, completely independent of vector.

This controls scenario D (adversarial correlation case).
"""

from __future__ import annotations

import numpy as np


def load(
    n: int = 50_000,
    dim: int = 96,
    correlation: str = "low",
    seed: int = 42,
) -> tuple[np.ndarray, list[dict]]:
    assert correlation in ("low", "high"), "correlation must be 'low' or 'high'"
    rng = np.random.default_rng(seed)

    raw = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    vectors = raw / norms

    if correlation == "low":
        # bucket has no relationship with vector direction
        buckets = rng.integers(0, 100, size=n)
    else:
        # bucket derived from which 10-dim block has the highest L2 norm
        # vectors in the same bucket are spatially close to each other
        blocks = dim // 10
        block_norms = np.array([
            np.linalg.norm(vectors[:, i*10:(i+1)*10], axis=1)
            for i in range(blocks)
        ]).T  # shape (n, blocks)
        dominant_block = np.argmax(block_norms, axis=1)  # 0..blocks-1
        # map block index to bucket 0..99
        buckets = (dominant_block * (100 // blocks)).astype(int)
        buckets = np.clip(buckets, 0, 99)

    metadata = [{"bucket": int(b)} for b in buckets]
    return vectors, metadata
