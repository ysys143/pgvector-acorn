"""Bulk load helper — COPY instead of executemany.

At 1M rows, row-by-row executemany is infeasible; COPY streams the whole batch
in one round trip.  Vectors are written in pgvector's text form ("[a,b,c]").
"""

from __future__ import annotations


def copy_load(cur, vectors, metadata) -> None:
    """COPY (bucket, embedding) rows into bench_items via the given cursor."""
    with cur.copy("COPY bench_items (bucket, embedding) FROM STDIN") as copy:
        for vec, m in zip(vectors, metadata):
            emb = "[" + ",".join("%.7g" % x for x in vec) + "]"
            copy.write_row((m["bucket"], emb))
