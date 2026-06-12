"""prefilter + exact target — the brute-force baseline for selective filters.

Filters first (WHERE bucket < N, accelerated by a btree on bucket), then sorts
the survivors by exact distance. Recall is always 1.0; cost scales with the
number of passing rows. This is the operating point ACORN/HNSW must beat: at
HIGH selectivity (few rows pass) it is fast AND exact, so approximate methods
only win when the filter is loose enough that scanning the survivors is costly.

No vector index and no ef knob — scenario A records a single point per
selectivity (ef=None). The pg_acorn Tier-1 hook is disabled so the planner uses
a plain bitmap/seq filter + sort, not an ACORN path.
"""

import psycopg
import numpy as np

from ._explain import explain_filtered as _explain_filtered
from ._bulk import copy_load as _copy_load


class PrefilterExactTarget:
    name = "prefilter_exact"

    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=True)

    def setup(self, vectors: np.ndarray, metadata: list[dict]) -> None:
        dim = vectors.shape[1]
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            # Disable the pg_acorn Tier-1 hook so this is a plain prefilter+sort,
            # not an ACORN custom scan.
            cur.execute("SET pg_acorn.enable_hook = off")
            cur.execute("DROP TABLE IF EXISTS bench_items CASCADE")
            cur.execute(f"""
                CREATE TABLE bench_items (
                    id      serial PRIMARY KEY,
                    bucket  int,
                    embedding vector({dim})
                )
            """)
            _copy_load(cur, vectors, metadata)
            # btree on bucket → index-accelerated prefilter (cheap at high
            # selectivity). No vector index → distance sort is exact.
            cur.execute("CREATE INDEX ON bench_items (bucket)")

    def query_filtered(self, query: np.ndarray, bucket_threshold: int, k: int) -> list[int]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM bench_items
                WHERE bucket < %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (bucket_threshold, query.tolist(), k),
            )
            return [row[0] for row in cur.fetchall()]

    def query_unfiltered(self, query: np.ndarray, k: int) -> list[int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bench_items ORDER BY embedding <=> %s::vector LIMIT %s",
                (query.tolist(), k),
            )
            return [row[0] for row in cur.fetchall()]

    def explain_filtered(self, query: np.ndarray, bucket_threshold: int, k: int) -> dict:
        return _explain_filtered(self.conn, query, bucket_threshold, k)

    # No set_ef_search and no force_index_scan: scenario A records one exact
    # point per selectivity and lets the planner pick the prefilter plan freely.

    def teardown(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS bench_items CASCADE")

    def close(self) -> None:
        self.conn.close()
