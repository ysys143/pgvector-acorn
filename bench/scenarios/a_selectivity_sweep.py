"""Scenario A: filter selectivity sweep — 1% to 80%.

Measures recall@10 and QPS for each target across selectivity levels.
ACORN should maintain recall >= 0.9 at all levels; pgvector recall collapses
at high selectivity.
"""

from __future__ import annotations

import time
import numpy as np


SELECTIVITIES = [1, 5, 10, 40, 80]  # percent — bucket < N gives N% selectivity
K = 10
N_QUERIES = 100


def compute_recall(result_ids: list[int], truth_ids: list[int]) -> float:
    return len(set(result_ids) & set(truth_ids)) / len(truth_ids)


def brute_force(target, query: np.ndarray, bucket_threshold: int, k: int) -> list[int]:
    """Ground truth via seq scan (caller must disable index scans externally)."""
    return target.query_filtered(query, bucket_threshold, k)


def run(target, queries: np.ndarray, ground_truth: dict) -> dict:
    results = {}

    for sel in SELECTIVITIES:
        recalls, latencies = [], []

        for i, q in enumerate(queries[:N_QUERIES]):
            t0 = time.perf_counter()
            ids = target.query_filtered(q, sel, K)
            latencies.append(time.perf_counter() - t0)

            truth = ground_truth[(sel, i)]
            recalls.append(compute_recall(ids, truth))

        results[sel] = {
            "recall_mean": float(np.mean(recalls)),
            "recall_min":  float(np.min(recalls)),
            "qps":         N_QUERIES / sum(latencies),
            "p99_ms":      float(np.percentile(latencies, 99) * 1000),
        }

    return results
