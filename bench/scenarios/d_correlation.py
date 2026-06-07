"""Scenario D: filter-query correlation adversarial case.

Tests recall under low correlation between filter predicate and query vector space.
Low correlation = the true nearest neighbors are spread across filter buckets,
so pre-filtering removes many true neighbors.

ACORN's neighbors-of-neighbors expansion handles this; naive inline skip fails.
"""

from __future__ import annotations

import numpy as np


K = 10
SELECTIVITY = 10   # 10% filter
N_QUERIES = 50


def compute_recall(result_ids: list[int], truth_ids: list[int]) -> float:
    if not truth_ids:
        return 1.0
    return len(set(result_ids) & set(truth_ids)) / len(truth_ids)


def run(target, queries: np.ndarray, ground_truth: dict,
        correlation_label: str) -> dict:
    """
    correlation_label: 'high' | 'low' — describes the fixture used.
    high: filter buckets are spatially clustered (filter and vector space aligned)
    low:  filter buckets are randomly assigned (no spatial correlation)
    """
    recalls, result_sizes = [], []

    for i, q in enumerate(queries[:N_QUERIES]):
        ids = target.query_filtered(q, SELECTIVITY, K)
        truth = ground_truth[(SELECTIVITY, i)]
        result_sizes.append(len(ids))
        if truth:
            recalls.append(compute_recall(ids, truth))

    return {
        "correlation":  correlation_label,
        "recall_mean":  float(np.mean(recalls)) if recalls else 0.0,
        "recall_min":   float(np.min(recalls)) if recalls else 0.0,
        "avg_returned": float(np.mean(result_sizes)),
    }
