"""Scenario C: incremental insert recall stability.

Inserts vectors in rounds after batch build and measures recall@10 after each round.
Validates acorn_build.c fixed-slot retry: recall must not degrade below 0.85.
pgvector's TODO bug is expected to show recall degradation at high insert volumes.
"""

from __future__ import annotations

import numpy as np


K = 10
SELECTIVITY = 5        # 5% filter
N_QUERIES = 50
BATCH_SIZE = 500       # rows per incremental round
N_ROUNDS = 4


def compute_recall(result_ids: list[int], truth_ids: list[int]) -> float:
    if not truth_ids:
        return 1.0
    return len(set(result_ids) & set(truth_ids)) / len(truth_ids)


def run(target, queries: np.ndarray,
        incremental_vectors: np.ndarray,
        incremental_meta: list[dict],
        ground_truth_fn) -> dict:
    """
    ground_truth_fn(query, selectivity) -> list[int]
        Called after each round to compute fresh brute-force truth
        (truth changes as new vectors are inserted).
    """
    results = {"rounds": []}

    def measure_round(label: str) -> dict:
        recalls = []
        for q in queries[:N_QUERIES]:
            truth = ground_truth_fn(q, SELECTIVITY)
            ids = target.query_filtered(q, SELECTIVITY, K)
            recalls.append(compute_recall(ids, truth))
        return {
            "label": label,
            "recall_mean": float(np.mean(recalls)),
            "recall_min":  float(np.min(recalls)),
        }

    results["rounds"].append(measure_round("baseline"))

    for rnd in range(N_ROUNDS):
        start = rnd * BATCH_SIZE
        end = start + BATCH_SIZE
        target.insert_batch(
            incremental_vectors[start:end],
            incremental_meta[start:end],
        )
        results["rounds"].append(measure_round(f"+{(rnd + 1) * BATCH_SIZE}"))

    return results
