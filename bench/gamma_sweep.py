"""Does acorn's correlated-fixture recall ceiling lift with higher gamma?

Phase D found Qdrant's filtered HNSW reaches 0.97-1.0 at sel 10/20% on the
correlated fixture while acorn g2 (gamma=2) caps ~0.91-0.94.  acorn allows
gamma up to 6 at m=16 (m_eff = m*gamma <= HNSW_MAX_M=100); we only benchmarked
gamma=2.  This sweep builds gamma=2/3/4 non-inline indexes on the SAME
correlated fixture (tv_items) and measures recall@10 at the money-cell
selectivities/ef — isolating gamma's effect on the recall ceiling.

Recall is deterministic, so host load is irrelevant here.

Run inside the bench postgres container:
  python3 -u /workspace/bench/gamma_sweep.py
"""
import json
import os

import numpy as np
import psycopg

from thesis_validation import K, make_fixture, exact_truth, qstr, SQL

DSN = os.environ.get("PG_DSN",
                     "host=/var/run/postgresql dbname=bench user=postgres")
OUT = os.path.join(os.path.dirname(__file__), "results_gamma_sweep.json")
SELS = [1, 2, 5, 10, 20]
EFS = [100, 200, 400, 800, 1600]
# gamma -> index name (gamma=2 reuses the existing tv_acorn_noinline)
GAMMAS = {2: "tv_acorn_noinline", 3: "tv_acorn_g3", 4: "tv_acorn_g4"}
ALL_ACORN = ["tv_acorn_idx", "tv_acorn_noinline", "tv_acorn_g3", "tv_acorn_g4"]


def build(cur, gamma, name):
    cur.execute(f"SELECT count(*) FROM pg_class WHERE relname = '{name}'")
    if cur.fetchone()[0]:
        print(f"[build] {name} (gamma={gamma}) exists, skip", flush=True)
        return
    print(f"[build] {name} gamma={gamma} ...", flush=True)
    cur.execute("SET maintenance_work_mem = '2GB'")
    cur.execute("SET max_parallel_maintenance_workers = 4")
    cur.execute("SET pg_acorn.build_seed = 42")
    cur.execute("ALTER TABLE tv_items SET (parallel_workers = 4)")
    cur.execute(
        f"CREATE INDEX {name} ON tv_items "
        f"USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops) "
        f"WITH (m=16, ef_construction=64, acorn_gamma={gamma}, "
        f"acorn_payload_edges=true, acorn_inline_vectors=false)")
    cur.execute(f"SELECT pg_size_pretty(pg_relation_size('{name}'))")
    print(f"[build] {name} done, size={cur.fetchone()[0]}", flush=True)


def recall_for(cur, keep, queries, truths, sel):
    """Measure recall@10 across ef for one index (others dropped in-txn)."""
    cur.execute("BEGIN")
    for idx in ALL_ACORN:
        if idx != keep:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
    out = []
    for ef in EFS:
        cur.execute(f"SET pg_acorn.ef_search = {ef}")
        recs = []
        for qi, q in enumerate(queries):
            cur.execute(SQL, (int(sel), qstr(q), K))
            got = {r[0] for r in cur.fetchall()}
            recs.append(len(got & truths[sel][qi]) / K)
        out.append({"ef": ef, "recall": round(float(np.mean(recs)), 4)})
    cur.execute("ROLLBACK")
    return out


def main():
    print("[fixture] correlated (seed 0) ...", flush=True)
    vecs, buckets, queries = make_fixture()
    truths = {s: [exact_truth(vecs, buckets, q, s) for q in queries]
              for s in SELS}

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    cur = conn.cursor()
    for gamma, name in GAMMAS.items():
        if gamma == 2:
            continue                       # tv_acorn_noinline already exists
        build(cur, gamma, name)

    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET enable_sort = off")
    cur.execute("SET pg_acorn.member_first = on")

    res = {"meta": {"fixture": "correlated seed0 250K", "k": K,
                    "note": "recall@10 deterministic; gamma isolates graph density"},
           "results": {}}
    for gamma, name in GAMMAS.items():
        res["results"][str(gamma)] = {}
        for sel in SELS:
            cells = recall_for(cur, name, queries, truths, sel)
            res["results"][str(gamma)][str(sel)] = cells
            ceil = max(c["recall"] for c in cells)
            print(f"[gamma={gamma} sel={sel}%] ceiling={ceil:.3f}  "
                  + " ".join(f"ef{c['ef']}={c['recall']:.3f}" for c in cells),
                  flush=True)
        with open(OUT, "w") as f:
            json.dump(res, f, indent=1)
    conn.close()
    print(f"\n[done] -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
