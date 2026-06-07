"""GloVe-like fixture with numerical range filter.

Generates random vectors approximating GloVe-25 statistics and attaches
a continuous 'score' field for range filter queries (score BETWEEN lo AND hi).

For real GloVe vectors: https://nlp.stanford.edu/projects/glove/
"""

from __future__ import annotations

import numpy as np


def load(
    n: int = 50_000,
    dim: int = 25,
    seed: int = 42,
) -> tuple[np.ndarray, list[dict]]:
    rng = np.random.default_rng(seed)

    # GloVe vectors are NOT unit-normalized; approximate with normal(0, 0.4)
    vectors = rng.normal(0.0, 0.4, size=(n, dim)).astype(np.float32)

    # Continuous score in [0, 1) for range filter: score < threshold
    # threshold=0.1 → ~10% selectivity, threshold=0.5 → ~50%, etc.
    scores = rng.uniform(0.0, 1.0, size=n)
    metadata = [{"score": float(s)} for s in scores]

    return vectors, metadata
