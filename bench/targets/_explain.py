"""Shared EXPLAIN (ANALYZE, BUFFERS) helpers for PostgreSQL-backed targets.

The SIGMOD 2026 FVS-in-PG paper argues that in a DBMS the real cost of filtered
vector search is *page access* (buffer lookups + locks), not distance
computation.  These helpers extract per-query logical page accesses
(shared hit + shared read) so the benchmark can report `pages_per_query`
alongside recall and QPS.
"""

from __future__ import annotations

import json


def sum_shared_blocks(plan_json) -> dict:
    """Sum shared buffer accesses from an EXPLAIN (FORMAT JSON) result.

    The root Plan node's buffer counters are cumulative over its children, so
    we read the root only and add the planning-phase buffers (present when
    BUFFERS is on, PG 13+).  Returns logical page accesses = hit + read.
    """
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)

    root = plan_json[0]
    ex = root.get("Plan", {})
    pl = root.get("Planning", {})

    hit = ex.get("Shared Hit Blocks", 0) + pl.get("Shared Hit Blocks", 0)
    read = ex.get("Shared Read Blocks", 0) + pl.get("Shared Read Blocks", 0)

    return {"pages_hit": hit, "pages_read": read, "pages_total": hit + read}


def scan_node_type(plan_json) -> str:
    """Return the node type that scans the relation (e.g. "Index Scan",
    "Seq Scan", "Custom Scan"), so callers can detect when the planner bypassed
    the vector index — which makes a page-access comparison meaningless.
    """
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)

    found = []

    def walk(node):
        nt = node.get("Node Type", "")
        if "Scan" in nt:
            found.append(nt)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan_json[0].get("Plan", {}))
    return found[0] if found else plan_json[0].get("Plan", {}).get("Node Type", "?")


def explain_filtered(conn, query, bucket_threshold: int, k: int) -> dict:
    """Run the filtered top-k query under EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    and return its per-query page-access counts plus the scan node type.

    Uses the same SQL as ``query_filtered`` so the measured plan matches the
    timed query exactly.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT id FROM bench_items
            WHERE bucket < %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (bucket_threshold, query.tolist(), k),
        )
        plan_json = cur.fetchone()[0]
    out = sum_shared_blocks(plan_json)
    out["plan"] = scan_node_type(plan_json)
    return out
