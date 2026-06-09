"""Scenario B: post-filter (CTE) recall degradation.

Quantifies the pgvector CTE workaround: how many times must you oversample
to achieve recall@10 >= 0.9? This proves top-k cannot be guaranteed.

Only runs against pgvector — other targets handle filtering natively.
"""

from __future__ import annotations

import numpy as np
import psycopg


MULTIPLIERS = [1, 2, 5, 10, 50]
K = 10
N_QUERIES = 50
SELECTIVITY = 5  # 5% filter — adversarial for post-filter


def run_cte(conn: psycopg.Connection, query: np.ndarray,
            bucket_threshold: int, k: int, multiplier: int) -> list[int]:
    """Simulate CTE post-filter: fetch k*multiplier, then filter."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH candidates AS (
                SELECT id, bucket
                FROM bench_items
                ORDER BY embedding <-> %s::vector
                LIMIT %s
            )
            SELECT id FROM candidates
            WHERE bucket < %s
            LIMIT %s
            """,
            (query.tolist(), k * multiplier, bucket_threshold, k),
        )
        return [row[0] for row in cur.fetchall()]


def run(pgvector_conn: psycopg.Connection,
        queries: np.ndarray,
        ground_truth: dict) -> dict:
    results = {}

    for mult in MULTIPLIERS:
        recalls, returned_counts = [], []

        for i, q in enumerate(queries[:N_QUERIES]):
            ids = run_cte(pgvector_conn, q, SELECTIVITY, K, mult)
            truth = ground_truth[(SELECTIVITY, i)]
            returned_counts.append(len(ids))
            if truth:
                recalls.append(len(set(ids) & set(truth)) / len(truth))

        results[mult] = {
            "recall_mean":      float(np.mean(recalls)) if recalls else 0.0,
            "avg_returned":     float(np.mean(returned_counts)),
            "topk_guaranteed":  all(c == K for c in returned_counts),
        }

    return results
