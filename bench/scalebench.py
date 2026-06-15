"""Scale benchmark: recall + latency DISTRIBUTION (median/p95/p99) + REAL
throughput (concurrency-swept QPS) for pg_acorn / pgvector / Qdrant on the loaded
Cohere fixture. Runs on the VM (psycopg + httpx, both via uv).

Reads queries.npy + truth_<N>.npz from --dir (produced by scale_load.py).
Index BUILD is done by the orchestrator (separate, timed); this measures.

  --mode latency     all configs, txn-drop isolation, sweep ef, recall+lat dist
  --mode throughput  one config (orchestrator isolated its index), conc sweep

  ~/.local/bin/uv run --with numpy --with "psycopg[binary]" --with httpx \
    python3 bench/scalebench.py --mode latency --n 100000 --dir ~/scale_data
"""
import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

PG = "host=127.0.0.1 user=postgres password=postgres dbname=bench"
QURL = "http://localhost:6333"
QCOLL = "cohere"
SELS = [1, 2, 5, 10, 20]
K = 10
ACORN_EFS = [40, 100, 200, 400, 800]
PGV_EFS = [40, 100, 200, 400, 800]
QD_EFS = [40, 100, 200, 400, 800]
CONC = [1, 4, 8, 16, 32]
DUR = 6.0

ALL_KNN = ["tv_acorn_g2p64", "tv_pgv_hnsw"]
PG_CONFIGS = {
    "acorn_g2p64":   {"keep": "tv_acorn_g2p64", "kind": "acorn"},
    "pgv_iterative": {"keep": "tv_pgv_hnsw",     "kind": "pgv_iterative"},
    "pgv_prefilter": {"keep": None,              "kind": "pgv_prefilter"},
}


def qstr(v):
    return "[" + ",".join(np.format_float_positional(x, trim="-") for x in v) + "]"


def dist(a):
    a = np.asarray(a)
    return {"med_ms": round(float(np.median(a)), 2),
            "p95_ms": round(float(np.percentile(a, 95)), 2),
            "p99_ms": round(float(np.percentile(a, 99)), 2),
            "min_ms": round(float(np.min(a)), 2)}


# ---------- Postgres ----------
def pg_gucs(cur, kind, ef):
    if kind == "acorn":
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET pg_acorn.member_first=on")
        cur.execute("SET pg_acorn.scan_code_cache=on")
        cur.execute("SET pg_acorn.scan_inline_vectors=on")
        if ef:
            cur.execute(f"SET pg_acorn.ef_search={ef}")
    elif kind == "pgv_iterative":
        cur.execute("SET enable_seqscan=off")
        cur.execute("SET hnsw.iterative_scan='strict_order'")
        cur.execute("SET hnsw.max_scan_tuples=40000")
        if ef:
            cur.execute(f"SET hnsw.ef_search={ef}")
    elif kind == "pgv_prefilter":
        cur.execute("SET enable_seqscan=on")
        cur.execute("SET enable_indexscan=on")


SQL = "SELECT id FROM tv_items WHERE bucket < %s ORDER BY embedding <=> %s LIMIT %s"


def pg_latency(queries, truth, out):
    import psycopg
    conn = psycopg.connect(PG, autocommit=True)
    qs = [qstr(q) for q in queries]
    for name, cfg in PG_CONFIGS.items():
        cur = conn.cursor()
        cur.execute("BEGIN")
        for idx in ALL_KNN:
            if idx != cfg["keep"]:
                cur.execute(f"DROP INDEX IF EXISTS {idx}")
        kind = cfg["kind"]
        pg_gucs(cur, kind, None)
        efs = [None] if kind == "pgv_prefilter" else (
            PGV_EFS if kind == "pgv_iterative" else ACORN_EFS)
        out[name] = {}
        for sel in SELS:
            cells = []
            for ef in efs:
                pg_gucs(cur, kind, ef)
                cur.execute(SQL, (sel, qs[0], K)); cur.fetchall()
                lats, recs = [], []
                for rep in range(3):
                    for i in range(len(qs)):
                        t0 = time.perf_counter()
                        cur.execute(SQL, (sel, qs[i], K))
                        ids = {r[0] for r in cur.fetchall()}
                        lats.append((time.perf_counter() - t0) * 1e3)
                        if rep == 0:
                            recs.append(len(ids & set(truth[str(sel)][i])) / K)
                cells.append({"ef": ef, "recall": round(float(np.mean(recs)), 4), **dist(lats)})
                print(f"  [{name} sel={sel}% ef={ef}] r={cells[-1]['recall']:.3f} "
                      f"med={cells[-1]['med_ms']} p99={cells[-1]['p99_ms']}", flush=True)
            out[name][str(sel)] = cells
        cur.execute("ROLLBACK")
    conn.close()


def _pg_tp_worker(args):
    import psycopg
    kind, ef, sel, qs, end_t = args
    conn = psycopg.connect(PG, autocommit=True)
    cur = conn.cursor()
    pg_gucs(cur, kind, ef)
    cur.execute(SQL, (sel, qs[0], K)); cur.fetchall()
    n = 0; qi = 0
    while time.time() < end_t:
        cur.execute(SQL, (sel, qs[qi % len(qs)], K)); cur.fetchall()
        qi += 1; n += 1
    conn.close()
    return n


def pg_throughput(name, sel, ef, queries):
    kind = PG_CONFIGS[name]["kind"]
    qs = [qstr(q) for q in queries]
    res = {}
    for nproc in CONC:
        end_t = time.time() + 1.0 + DUR
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=nproc) as ex:
            counts = list(ex.map(_pg_tp_worker, [(kind, ef, sel, qs, end_t)] * nproc))
        wall = time.time() - t0
        res[str(nproc)] = {"qps": round(sum(counts) / wall, 1)}
        print(f"  [{name} tp sel={sel}% conc={nproc}] qps={res[str(nproc)]['qps']:.0f}", flush=True)
    return res


# ---------- Qdrant ----------
def qd_search(c, q, sel, ef):
    r = c.post(f"/collections/{QCOLL}/points/search", json={
        "vector": q.tolist(), "limit": K,
        "filter": {"must": [{"key": "bucket", "range": {"lt": sel}}]},
        "params": {"hnsw_ef": ef}})
    r.raise_for_status()
    return {h["id"] for h in r.json()["result"]}


def qd_latency(queries, truth, out):
    import httpx
    c = httpx.Client(base_url=QURL, timeout=600.0)
    for sel in SELS:
        cells = []
        for ef in QD_EFS:
            qd_search(c, queries[0], sel, ef)
            lats, recs = [], []
            for rep in range(3):
                for i in range(len(queries)):
                    t0 = time.perf_counter()
                    got = qd_search(c, queries[i], sel, ef)
                    lats.append((time.perf_counter() - t0) * 1e3)
                    if rep == 0:
                        recs.append(len(got & set(truth[str(sel)][i])) / K)
            cells.append({"ef": ef, "recall": round(float(np.mean(recs)), 4), **dist(lats)})
            print(f"  [qdrant sel={sel}% ef={ef}] r={cells[-1]['recall']:.3f} "
                  f"med={cells[-1]['med_ms']} p99={cells[-1]['p99_ms']}", flush=True)
        out[str(sel)] = cells
    c.close()


def _qd_tp_worker(args):
    import httpx
    sel, ef, qlist, end_t = args
    c = httpx.Client(base_url=QURL, timeout=600.0)
    qd_search(c, np.asarray(qlist[0], dtype=np.float32), sel, ef)
    n = 0; qi = 0
    while time.time() < end_t:
        qd_search(c, np.asarray(qlist[qi % len(qlist)], dtype=np.float32), sel, ef)
        qi += 1; n += 1
    c.close()
    return n


def qd_throughput(sel, ef, queries):
    qlist = [q.tolist() for q in queries]
    res = {}
    for nproc in CONC:
        end_t = time.time() + 1.0 + DUR
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=nproc) as ex:
            counts = list(ex.map(_qd_tp_worker, [(sel, ef, qlist, end_t)] * nproc))
        res[str(nproc)] = {"qps": round(sum(counts) / (time.time() - t0), 1)}
        print(f"  [qdrant tp sel={sel}% conc={nproc}] qps={res[str(nproc)]['qps']:.0f}", flush=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["latency", "throughput"], required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--dir", default=os.path.expanduser("~/scale_data"))
    ap.add_argument("--engine", default="all")     # latency: all|pg|qdrant
    ap.add_argument("--config")                    # throughput: config or 'qdrant'
    ap.add_argument("--sel", type=int)             # throughput
    ap.add_argument("--ef", type=int)              # throughput
    ap.add_argument("--out")
    args = ap.parse_args()

    queries = np.load(os.path.join(args.dir, "queries.npy")).astype(np.float32)
    tz = np.load(os.path.join(args.dir, f"truth_{args.n}.npz"))
    truth = {k: tz[k].tolist() for k in tz.files}
    out_path = args.out or os.path.join(args.dir, f"results_scale_{args.n}.json")
    res = json.load(open(out_path)) if os.path.exists(out_path) else {"n": args.n}

    if args.mode == "latency":
        if args.engine in ("all", "pg"):
            res.setdefault("pg_latency", {})
            pg_latency(queries, truth, res["pg_latency"])
        if args.engine in ("all", "qdrant"):
            res.setdefault("qdrant_latency", {})
            qd_latency(queries, truth, res["qdrant_latency"])
    else:
        ef = args.ef if args.ef and args.ef > 0 else None
        if args.config == "qdrant":
            r = qd_throughput(args.sel, ef or 100, queries)
        else:
            r = pg_throughput(args.config, args.sel, ef, queries)
        res.setdefault("throughput", {})[args.config] = {"sel": args.sel, "ef": ef, "by_conc": r}

    with open(out_path, "w") as f:
        json.dump(res, f, indent=1)
    print(f"[done] -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
