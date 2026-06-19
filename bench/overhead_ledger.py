"""Overhead accounting ledger for pg_acorn vs Qdrant.

Directive: NO unattributed "Postgres overhead". Every microsecond of the gap
vs Qdrant on the n=30K dim=128 correlated workload must land on a measured
component line:

  S1  per-distance cost via SQL/fmgr vs numpy float32 SIMD floor
  S2  per-buffer-access + per-tuple cost (least squares over bitmap count(*)
      scans at several selectivities: exec_ms = a*pages + b*rows + c)
  S3  bitmap-prefilter KNN decomposition (the sel=1% operating point):
      node-exclusive times from EXPLAIN ANALYZE + parse/plan/protocol split
      from prepared vs unprepared client latency
  S4  acorn tier2 scan floor (sel=40 gamma=4 operating point): model
      buffers*t_buf + distances*t_dist + fetches*t_fetch vs measured;
      residual must be <30% or probed (LIMIT 10 vs 100)
  S5  Qdrant unit cost: latency vs params.hnsw_ef sweep on the same data

Self-contained (numpy ground truth + psycopg + httpx), own table/collection
(ledger_items), dropped at the end.  Run inside the bench container:

  docker compose -f docker/docker-compose.yml --profile bench run --rm bench \
      python3 -u bench/overhead_ledger.py
"""

import json
import os

# OpenBLAS thread spin-waiting is pathological inside the Docker VM (a
# 30000x128 float32 matvec takes 150 ms multi-threaded vs 0.46 ms single-
# threaded).  Pin to 1 thread BEFORE importing numpy so the SIMD floor
# measurement is honest.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import statistics
import time

import httpx
import numpy as np
import psycopg

DSN = "postgresql://postgres:postgres@postgres/bench"
QDRANT = "http://qdrant:6333"
TABLE = "ledger_items"
COLLECTION = "ledger_items"
OUT = os.environ.get("LEDGER_OUT", "bench/results_overhead_ledger.json")

N = int(os.environ.get("LEDGER_N", 30_000))
DIM, K = 128, 10
NQ = int(os.environ.get("LEDGER_NQ", 30))
GAMMA = int(os.environ.get("LEDGER_GAMMA", 4))
SEED = 42  # matches bench/fixtures/synthetic.py
REPS = int(os.environ.get("LEDGER_REPS", 7))  # client-latency repeats

results: dict = {
    "config": {
        "n": N,
        "dim": DIM,
        "nq": NQ,
        "k": K,
        "gamma": GAMMA,
        "seed": SEED,
        "reps": REPS,
        "correlation": "high",
    }
}


# ---------------------------------------------------------------------------
# data: identical to fixtures/synthetic.py correlation='high'
# ---------------------------------------------------------------------------
def make_data():
    rng = np.random.default_rng(SEED)
    raw = rng.standard_normal((N, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    blocks = DIM // 10
    block_norms = np.array(
        [np.linalg.norm(vecs[:, i * 10 : (i + 1) * 10], axis=1) for i in range(blocks)]
    ).T
    dominant = np.argmax(block_norms, axis=1)
    buckets = np.clip((dominant * (100 // blocks)).astype(int), 0, 99)
    return vecs, buckets


VECS, BUCKETS = make_data()
QUERIES = VECS[:NQ]


def qstr(q):
    return "[" + ",".join(f"{x:.6f}" for x in q) + "]"


def knn_sql(th: int, q, k: int) -> str:
    """Literal SQL for the filtered KNN query (for EXPLAIN, no params)."""
    return (
        f"SELECT id FROM {TABLE} WHERE bucket < {th} "
        f"ORDER BY embedding <=> '{qstr(q)}'::vector LIMIT {k}"
    )


def exact_truth(q, thresh):
    idx = np.where(BUCKETS < thresh)[0]
    sims = VECS[idx] @ q
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())


def med(xs):
    return statistics.median(xs)


# ---------------------------------------------------------------------------
# plan-tree helpers
# ---------------------------------------------------------------------------
def node_exclusive(plan):
    """Flatten plan tree -> [(node type, inclusive_ms, exclusive_ms, extra)]."""
    rows = []

    def walk(node):
        loops = node.get("Actual Loops", 1)
        incl = node.get("Actual Total Time", 0.0) * loops
        child_sum = 0.0
        for ch in node.get("Plans", []):
            child_sum += walk(ch)
        rows.append(
            {
                "node": node.get("Node Type", "?"),
                "index": node.get("Index Name", ""),
                "incl_ms": incl,
                "excl_ms": incl - child_sum,
                "rows": node.get("Actual Rows", 0) * loops,
                "removed": node.get("Rows Removed by Filter", 0) * loops,
                "output": node.get("Output", []),
            }
        )
        return incl

    walk(plan[0]["Plan"])
    rows.reverse()  # root first
    return rows


def run_explain(cur, sql, reps=3):
    """EXPLAIN (ANALYZE, BUFFERS, VERBOSE) reps times; median by exec time."""
    outs = []
    for _ in range(reps):
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) " + sql)
        outs.append(cur.fetchone()[0])
    outs.sort(key=lambda p: p[0]["Execution Time"])
    return outs[len(outs) // 2]


def buffers_of(plan):
    p = plan[0]["Plan"]
    pl = plan[0].get("Planning", {})
    return (
        p.get("Shared Hit Blocks", 0) + p.get("Shared Read Blocks", 0),
        pl.get("Shared Hit Blocks", 0) + pl.get("Shared Read Blocks", 0),
    )


def client_lat_ms(cur, sql, params, prepare, reps=REPS):
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        cur.execute(sql, params, prepare=prepare)
        cur.fetchall()
        ts.append((time.perf_counter() - t0) * 1e3)
    return ts


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def setup_pg(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute(
            f"CREATE TABLE {TABLE} "
            f"(id serial PRIMARY KEY, bucket int, embedding vector({DIM}))"
        )
        print(f"loading {N} rows...", flush=True)
        with cur.copy(f"COPY {TABLE} (bucket, embedding) FROM STDIN") as cp:
            for i in range(N):
                cp.write_row((int(BUCKETS[i]), qstr(VECS[i])))
        print(
            f"building acorn_hnsw gamma={GAMMA} (this is the slow part)...", flush=True
        )
        t0 = time.perf_counter()
        cur.execute(f"""CREATE INDEX ON {TABLE}
                        USING acorn_hnsw (embedding vector_cosine_ops,
                                          bucket int4_acorn_ops)
                        WITH (m = 16, ef_construction = 64,
                              acorn_gamma = {GAMMA})""")
        build_s = time.perf_counter() - t0
        cur.execute(f"CREATE INDEX ON {TABLE} (bucket)")
        cur.execute(f"VACUUM ANALYZE {TABLE}")
        # warm everything into shared_buffers / page cache
        cur.execute(f"SELECT count(*) FROM {TABLE}")
        cur.execute(
            f"SELECT sum(embedding <=> %s::vector) FROM {TABLE}", (qstr(QUERIES[0]),)
        )
        cur.execute(f"SELECT pg_relation_size('{TABLE}')")
        results["config"]["acorn_build_s"] = round(build_s, 1)
    sels = {th: float((BUCKETS < th).mean()) for th in (1, 10, 40)}
    results["config"]["actual_selectivity"] = sels
    print(f"actual selectivity of thresholds: {sels}", flush=True)


# ---------------------------------------------------------------------------
# S1 — per-distance cost (fmgr/SQL) vs numpy SIMD floor
# ---------------------------------------------------------------------------
def s1_distance(conn):
    print("\n=== S1: per-distance cost ===", flush=True)
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        base, dist = [], []
        for rep in range(9):
            t0 = time.perf_counter()
            cur.execute(f"SELECT count(*) FROM {TABLE}")
            cur.fetchall()
            base.append(time.perf_counter() - t0)
        for q in QUERIES[:9]:
            t0 = time.perf_counter()
            cur.execute(
                f"SELECT sum(embedding <=> %s::vector) FROM {TABLE}",
                (qstr(q),),
                prepare=True,
            )
            cur.fetchall()
            dist.append(time.perf_counter() - t0)
        cur.execute("RESET max_parallel_workers_per_gather")
    t_base, t_dist = med(base), med(dist)
    ns_sql = (t_dist - t_base) / N * 1e9

    # numpy float32 SIMD floor (batched matvec over the same 30000x128)
    np_ts = []
    for q in QUERIES[:9]:
        t0 = time.perf_counter()
        _ = VECS @ q  # cosine on unit vectors
        np_ts.append(time.perf_counter() - t0)
    ns_np = med(np_ts) / N * 1e9

    s1 = {
        "seqscan_count_ms": t_base * 1e3,
        "seqscan_distsum_ms": t_dist * 1e3,
        "ns_per_distance_sql_fmgr": ns_sql,
        "ns_per_distance_numpy_simd": ns_np,
        "fmgr_executor_tax_x": ns_sql / ns_np,
    }
    results["s1_distance"] = s1
    print(f"  count(*) pass      : {t_base * 1e3:8.2f} ms")
    print(f"  sum(<=>) pass      : {t_dist * 1e3:8.2f} ms")
    print(f"  SQL distance       : {ns_sql:8.1f} ns/dist (fmgr+detoast+executor)")
    print(f"  numpy SIMD floor   : {ns_np:8.1f} ns/dist")
    print(f"  fmgr/executor tax  : {s1['fmgr_executor_tax_x']:.1f}x", flush=True)
    return s1


# ---------------------------------------------------------------------------
# S2 — per-buffer-access and per-tuple cost (least squares)
# ---------------------------------------------------------------------------
def s2_page(conn):
    print("\n=== S2: per-page / per-tuple cost ===", flush=True)
    # buckets are multiples of (100 // (DIM//10)); thresholds chosen so each
    # adds one bucket block -> rows AND touched pages both vary (conditioning).
    step = 100 // (DIM // 10)
    ths = [1] + [b * step + 1 for b in range(1, 7)] + [100]
    pts = []
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_indexscan = off")
        cur.execute("SET enable_indexonlyscan = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")
        for th in ths:
            sql = f"SELECT count(*) FROM {TABLE} WHERE bucket < {th}"
            plan = run_explain(cur, sql, reps=7)
            exec_ms = plan[0]["Execution Time"]
            bufs, _ = buffers_of(plan)
            nodes = node_exclusive(plan)
            scan = next(n for n in nodes if "Bitmap Heap" in n["node"])
            pts.append(
                {
                    "threshold": th,
                    "exec_ms": exec_ms,
                    "buffers": bufs,
                    "rows": scan["rows"],
                    "plan": [n["node"] for n in nodes],
                }
            )
            print(
                f"  th={th:>3} rows={scan['rows']:>6} bufs={bufs:>5} "
                f"exec={exec_ms:7.2f} ms  ({' -> '.join(pts[-1]['plan'])})",
                flush=True,
            )
        # one seqscan point: same rows/pages relation, no bitmap machinery
        cur.execute("SET enable_seqscan = on")
        cur.execute("SET enable_bitmapscan = off")
        plan = run_explain(cur, f"SELECT count(*) FROM {TABLE}", reps=7)
        seq_bufs, _ = buffers_of(plan)
        seq_pt = {
            "threshold": "seq100",
            "exec_ms": plan[0]["Execution Time"],
            "buffers": seq_bufs,
            "rows": N,
            "plan": ["Seq Scan"],
        }
        print(
            f"  seqscan rows={N:>6} bufs={seq_bufs:>5} "
            f"exec={seq_pt['exec_ms']:7.2f} ms",
            flush=True,
        )
        cur.execute("RESET enable_bitmapscan")
        for g in (
            "enable_seqscan",
            "enable_indexscan",
            "enable_indexonlyscan",
            "max_parallel_workers_per_gather",
        ):
            cur.execute(f"RESET {g}")
    # th=100 (bitmap over the whole table) is anomalous (lossy-bitmap /
    # tbm pathology measured at ~8x the seqscan time); exclude it from the fit
    # but keep the data point on record.
    fit_pts = [p for p in pts if p["threshold"] != 100]
    A = np.array([[p["buffers"], p["rows"], 1.0] for p in fit_pts])
    y = np.array([p["exec_ms"] for p in fit_pts])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coef
    r2 = 1 - float(((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum())
    us_buf, us_row = coef[0] * 1e3, coef[1] * 1e3
    s2 = {
        "points": pts + [seq_pt],
        "us_per_buffer_access": us_buf,
        "us_per_tuple": us_row,
        "intercept_ms": coef[2],
        "r2": r2,
    }
    results["s2_page"] = s2
    print(
        f"  least squares (bitmap pts): {us_buf:.2f} us/buffer-access, "
        f"{us_row:.3f} us/tuple, intercept {coef[2]:.2f} ms, R2={r2:.3f}",
        flush=True,
    )
    return s2


# ---------------------------------------------------------------------------
# S3 — bitmap-prefilter KNN decomposition (threshold=1 operating point)
# ---------------------------------------------------------------------------
def s3_bitmap_knn(conn, s1):
    print("\n=== S3: bitmap prefilter KNN decomposition (threshold=1) ===", flush=True)
    th = 1
    sql_t = (
        f"SELECT id FROM {TABLE} WHERE bucket < %s "
        f"ORDER BY embedding <=> %s::vector LIMIT %s"
    )
    node_acc: dict = {}
    exec_ms, plan_ms, bufs_l, dist_rows = [], [], [], []
    sample_plan = None
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_indexscan = off")  # forbid acorn; force bitmap
        cur.execute("SET max_parallel_workers_per_gather = 0")
        for qi, q in enumerate(QUERIES):
            plan = run_explain(cur, knn_sql(th, q, K), reps=3)
            if sample_plan is None:
                sample_plan = plan
            exec_ms.append(plan[0]["Execution Time"])
            plan_ms.append(plan[0].get("Planning Time", 0.0))
            b, _ = buffers_of(plan)
            bufs_l.append(b)
            dist_nodes = []
            for nd in node_exclusive(plan):
                key = nd["node"] + (" " + nd["index"] if nd["index"] else "")
                node_acc.setdefault(key, []).append(nd["excl_ms"])
                if any("<=>" in o for o in nd["output"]):
                    dist_nodes.append(nd["rows"])
            # the distance expr appears in every node above the scan; it is
            # EVALUATED once per scan output row -> take the max-rows node
            if dist_nodes:
                dist_rows.append(max(dist_nodes))

        # client-side latency: prepared vs unprepared (parse/plan share)
        lat_prep, lat_unprep = [], []
        for q in QUERIES:
            lat_prep += client_lat_ms(cur, sql_t, (th, qstr(q), K), True, 3)
        for q in QUERIES:
            lat_unprep += client_lat_ms(cur, sql_t, (th, qstr(q), K), False, 3)
        for g in (
            "enable_seqscan",
            "enable_indexscan",
            "max_parallel_workers_per_gather",
        ):
            cur.execute(f"RESET {g}")

    nodes_med = {k: med(v) for k, v in node_acc.items()}
    rows_dist = med(dist_rows) if dist_rows else 0
    dist_eval_ms = rows_dist * s1["ns_per_distance_sql_fmgr"] / 1e6

    s3 = {
        "threshold": th,
        "exec_ms_median": med(exec_ms),
        "planning_ms_median": med(plan_ms),
        "buffers_median": med(bufs_l),
        "node_exclusive_ms_median": nodes_med,
        "rows_distance_evaluated": rows_dist,
        "distance_eval_ms_est": dist_eval_ms,
        "client_ms_prepared_median": med(lat_prep),
        "client_ms_unprepared_median": med(lat_unprep),
        "parse_plan_ms": med(lat_unprep) - med(lat_prep),
        "protocol_ms": med(lat_prep) - med(exec_ms),
        "sample_plan": sample_plan,
    }
    results["s3_bitmap_knn"] = s3
    print(
        f"  exec={s3['exec_ms_median']:.2f} ms  planning={s3['planning_ms_median']:.2f} ms  "
        f"buffers={s3['buffers_median']:.0f}"
    )
    for k, v in sorted(nodes_med.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<45s} {v:7.2f} ms excl")
    print(
        f"  distance eval (est, {rows_dist:.0f} rows x "
        f"{s1['ns_per_distance_sql_fmgr']:.0f} ns) = {dist_eval_ms:.2f} ms"
    )
    print(
        f"  client prepared={s3['client_ms_prepared_median']:.2f} ms  "
        f"unprepared={s3['client_ms_unprepared_median']:.2f} ms  "
        f"-> parse/plan={s3['parse_plan_ms']:.2f} ms  "
        f"protocol/fetch={s3['protocol_ms']:.2f} ms",
        flush=True,
    )
    return s3


# ---------------------------------------------------------------------------
# S4 — acorn tier2 scan floor (threshold=40, gamma=4)
# ---------------------------------------------------------------------------
def s4_acorn(conn, s1, s2):
    print("\n=== S4: acorn tier2 scan floor (threshold=40) ===", flush=True)
    th = 40
    sql_t = (
        f"SELECT id FROM {TABLE} WHERE bucket < %s "
        f"ORDER BY embedding <=> %s::vector LIMIT %s"
    )
    sweep = []
    pairs = []  # per-query (buffers, exec_ms) for the slope cross-check
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_sort = off")  # forbid btree+sort path
        cur.execute("SET max_parallel_workers_per_gather = 0")
        for ef in (10, 20, 40, 80, 160, 320):
            cur.execute(f"SET pg_acorn.ef_search = {ef}")
            ex_l, buf_l, fetched_l, removed_l, rec_l, lat_l = [], [], [], [], [], []
            plan_node = None
            for q in QUERIES:
                sql = knn_sql(th, q, K)
                plan = run_explain(cur, sql, reps=3)
                ex_l.append(plan[0]["Execution Time"])
                b, _ = buffers_of(plan)
                buf_l.append(b)
                pairs.append((b, plan[0]["Execution Time"]))
                nodes = node_exclusive(plan)
                scan = next((n for n in nodes if n["node"] == "Index Scan"), None)
                if scan:
                    plan_node = "Index Scan " + scan["index"]
                    fetched_l.append(scan["rows"] + scan["removed"])
                    removed_l.append(scan["removed"])
                cur.execute(sql)
                ids = {r[0] for r in cur.fetchall()}
                rec_l.append(len(ids & exact_truth(q, th)) / K)
                lat_l += client_lat_ms(cur, sql_t, (th, qstr(q), K), True, 3)
            sweep.append(
                {
                    "ef": ef,
                    "exec_ms": med(ex_l),
                    "buffers": med(buf_l),
                    "fetched": med(fetched_l),
                    "removed": med(removed_l),
                    "recall": float(np.mean(rec_l)),
                    "client_ms": med(lat_l),
                    "plan": plan_node,
                }
            )
            print(
                f"  ef={ef:>4} exec={med(ex_l):7.2f} ms client={med(lat_l):7.2f} ms "
                f"bufs={med(buf_l):>6.0f} fetched={med(fetched_l):>4.0f} "
                f"recall={np.mean(rec_l):.3f}  [{plan_node}]",
                flush=True,
            )

        # LIMIT probe at the operating ef: heap-fetch / visibility cost per row
        op = next((s for s in sweep if s["recall"] >= 0.95), sweep[-1])
        cur.execute(f"SET pg_acorn.ef_search = {op['ef']}")
        probe = {}
        for lim in (10, 100):
            ex_l, buf_l, fetched_l = [], [], []
            for q in QUERIES[:10]:
                plan = run_explain(cur, knn_sql(th, q, lim), reps=3)
                ex_l.append(plan[0]["Execution Time"])
                b, _ = buffers_of(plan)
                buf_l.append(b)
                nodes = node_exclusive(plan)
                scan = next(n for n in nodes if n["node"] == "Index Scan")
                fetched_l.append(scan["rows"] + scan["removed"])
            probe[lim] = {
                "exec_ms": med(ex_l),
                "buffers": med(buf_l),
                "fetched": med(fetched_l),
            }
            print(
                f"  LIMIT {lim:>3}: exec={med(ex_l):7.2f} ms bufs={med(buf_l):6.0f} "
                f"fetched={med(fetched_l):5.0f}",
                flush=True,
            )
        for g in (
            "enable_seqscan",
            "enable_bitmapscan",
            "enable_sort",
            "max_parallel_workers_per_gather",
            "pg_acorn.ef_search",
        ):
            cur.execute(f"RESET {g}")

    d_fetch = probe[100]["fetched"] - probe[10]["fetched"]
    d_ms = probe[100]["exec_ms"] - probe[10]["exec_ms"]
    us_per_emitted = (d_ms / d_fetch * 1e3) if d_fetch else None

    # cross-check: per-query regression exec_ms ~ buffers across the sweep.
    # By the scan code each NEW neighbor costs 2 buffer accesses + 1 distance,
    # so the slope bundles t_buf + ~0.5*t_dist per buffer access.
    Ap = np.array([[b, 1.0] for b, _ in pairs])
    yp = np.array([e for _, e in pairs])
    sc, *_ = np.linalg.lstsq(Ap, yp, rcond=None)
    slope_us = sc[0] * 1e3
    t_buf_acorn_us = slope_us - 0.5 * s1["ns_per_distance_sql_fmgr"] / 1e3

    # floor model at the operating point.
    # buffer accesses per the scan code: expansion = 2 bufs (load_element +
    # get_neighbors); each NEW neighbor = 2 bufs (acorn_distance +
    # load_element); each emitted row = 1 heap buf (executor fetch).
    # => distances D ~= (buffers - 2*E - fetched) / 2, with E <= ef.
    E = op["ef"]
    D = max((op["buffers"] - 2 * E - op["fetched"]) / 2, 0)
    t_buf_us = s2["us_per_buffer_access"]
    if t_buf_us <= 0:  # ill-conditioned S2 fit -> use acorn slope
        t_buf_us = t_buf_acorn_us
    floor_ms = (
        op["buffers"] * t_buf_us
        + D * s1["ns_per_distance_sql_fmgr"] / 1e3
        + op["fetched"] * s2["us_per_tuple"]
    ) / 1e3
    resid_ms = op["exec_ms"] - floor_ms
    s4 = {
        "sweep": sweep,
        "operating_point": op,
        "limit_probe": probe,
        "us_per_emitted_row": us_per_emitted,
        "slope_us_per_buffer": slope_us,
        "t_buf_acorn_us": t_buf_acorn_us,
        "model": {
            "expansions_assumed": E,
            "distances_est": D,
            "t_buf_us_used": t_buf_us,
            "floor_ms": floor_ms,
            "measured_ms": op["exec_ms"],
            "residual_ms": resid_ms,
            "residual_share": resid_ms / op["exec_ms"],
        },
    }
    results["s4_acorn"] = s4
    print(
        f"  slope cross-check: {slope_us:.2f} us per buffer access "
        f"(=> t_buf ~ {t_buf_acorn_us:.2f} us after 0.5*t_dist)",
        flush=True,
    )
    print(
        f"  floor model @ef={E}: bufs {op['buffers']:.0f} x "
        f"{t_buf_us:.2f} us + dist {D:.0f} x "
        f"{s1['ns_per_distance_sql_fmgr'] / 1e3:.2f} us + fetch {op['fetched']:.0f} x "
        f"{s2['us_per_tuple']:.3f} us = {floor_ms:.2f} ms"
    )
    print(
        f"  measured exec = {op['exec_ms']:.2f} ms -> residual "
        f"{resid_ms:.2f} ms ({100 * resid_ms / op['exec_ms']:.0f}%)",
        flush=True,
    )
    return s4


# ---------------------------------------------------------------------------
# S5 — Qdrant unit cost on the same workload
# ---------------------------------------------------------------------------
def s5_qdrant():
    print("\n=== S5: Qdrant unit cost (hnsw_ef sweep) ===", flush=True)
    c = httpx.Client(base_url=QDRANT, timeout=300.0)
    c.delete(f"/collections/{COLLECTION}")
    c.put(
        f"/collections/{COLLECTION}",
        json={
            "vectors": {"size": DIM, "distance": "Cosine"},
            "hnsw_config": {"m": 16, "ef_construct": 64},
        },
    ).raise_for_status()
    c.put(
        f"/collections/{COLLECTION}/index",
        json={"field_name": "bucket", "field_schema": "integer"},
    ).raise_for_status()
    B = 2000
    for s0 in range(0, N, B):
        e0 = min(s0 + B, N)
        pts = [
            {
                "id": i + 1,
                "vector": VECS[i].tolist(),
                "payload": {"bucket": int(BUCKETS[i])},
            }
            for i in range(s0, e0)
        ]
        c.put(
            f"/collections/{COLLECTION}/points" + ("?wait=true" if e0 >= N else ""),
            json={"points": pts},
        ).raise_for_status()
    # wait for indexing to settle
    for _ in range(120):
        st = c.get(f"/collections/{COLLECTION}").json()["result"]["status"]
        if st == "green":
            break
        time.sleep(1)

    out = {}
    for th in (1, 40):
        rows = []
        for ef in (8, 16, 32, 64, 128, 256, 512):
            lat, rec = [], []
            # warmup this (threshold, ef) combination
            for q in QUERIES[:5]:
                c.post(
                    f"/collections/{COLLECTION}/points/search",
                    json={
                        "vector": q.tolist(),
                        "limit": K,
                        "filter": {"must": [{"key": "bucket", "range": {"lt": th}}]},
                        "params": {"hnsw_ef": ef},
                    },
                )
            for q in QUERIES:
                body = {
                    "vector": q.tolist(),
                    "limit": K,
                    "filter": {"must": [{"key": "bucket", "range": {"lt": th}}]},
                    "params": {"hnsw_ef": ef},
                }
                for _ in range(REPS):
                    t0 = time.perf_counter()
                    r = c.post(f"/collections/{COLLECTION}/points/search", json=body)
                    lat.append((time.perf_counter() - t0) * 1e3)
                ids = {h["id"] for h in r.json()["result"]}
                rec.append(len(ids & exact_truth(q, th)) / K)
            rows.append(
                {"hnsw_ef": ef, "client_ms": med(lat), "recall": float(np.mean(rec))}
            )
            print(
                f"  th={th:>2} hnsw_ef={ef:>4} client={med(lat):6.2f} ms "
                f"recall={np.mean(rec):.3f}",
                flush=True,
            )
        # per-ef-unit slope (us per candidate-list slot ~ per-hop proxy)
        A = np.array([[r["hnsw_ef"], 1.0] for r in rows])
        y = np.array([r["client_ms"] for r in rows])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        out[th] = {
            "sweep": rows,
            "us_per_ef_unit": coef[0] * 1e3,
            "intercept_ms": coef[1],
        }
        print(
            f"  th={th}: {coef[0] * 1e3:.2f} us per hnsw_ef unit, "
            f"intercept {coef[1]:.2f} ms",
            flush=True,
        )
    c.delete(f"/collections/{COLLECTION}")
    c.close()
    results["s5_qdrant"] = out
    return out


# ---------------------------------------------------------------------------
# ledger assembly
# ---------------------------------------------------------------------------
def print_ledger(s1, s2, s3, s4, s5):
    print("\n" + "=" * 78)
    print("OVERHEAD LEDGER  (n=30K dim=128 correlated, hot cache, single client)")
    print("=" * 78)

    # --- workload A: threshold=1 (the '1%' label), PG bitmap vs Qdrant ---
    qd1 = min(
        (r for r in s5[1]["sweep"] if r["recall"] >= 0.99),
        key=lambda r: r["client_ms"],
        default=s5[1]["sweep"][-1],
    )
    pg_total = s3["client_ms_unprepared_median"]
    gap = pg_total - qd1["client_ms"]
    print(
        f"\n[A] threshold=1: PG bitmap-prefilter {pg_total:.2f} ms/q vs "
        f"Qdrant {qd1['client_ms']:.2f} ms/q (hnsw_ef={qd1['hnsw_ef']}, "
        f"recall={qd1['recall']:.3f}) -> gap {gap:.2f} ms"
    )
    hdr = f"  {'component':<38s} {'ms/query':>9s} {'% of PG total':>14s}"
    print(hdr)
    print("  " + "-" * len(hdr))
    dist_ms = s3["distance_eval_ms_est"]
    # subtract distance estimate from the node that computed it
    rows2 = []
    for k, v in sorted(s3["node_exclusive_ms_median"].items(), key=lambda kv: -kv[1]):
        if "Bitmap Heap Scan" in k:
            rows2.append((k + " (pages+visibility+deform)", v - dist_ms))
            rows2.append(
                (
                    f"distance eval (fmgr, {s3['rows_distance_evaluated']:.0f} rows)",
                    dist_ms,
                )
            )
        else:
            rows2.append((k, v))
    rows2.append(("planner (Planning Time)", s3["planning_ms_median"]))
    rows2.append(("parse/plan (unprepared - prepared)", s3["parse_plan_ms"]))
    rows2.append(("protocol+fetch (client - exec)", s3["protocol_ms"]))
    for k, v in rows2:
        print(f"  {k:<38s} {v:9.2f} {100 * v / pg_total:13.0f}%")
    print(f"  {'TOTAL (client, unprepared)':<38s} {pg_total:9.2f}")

    # --- workload B: threshold=40, acorn tier2 vs Qdrant ---
    op = s4["operating_point"]
    qd40 = min(
        (r for r in s5[40]["sweep"] if r["recall"] >= op["recall"]),
        key=lambda r: r["client_ms"],
        default=s5[40]["sweep"][-1],
    )
    m = s4["model"]
    pg_total = op["client_ms"]
    print(
        f"\n[B] threshold=40: acorn tier2 g{GAMMA} ef={op['ef']} "
        f"recall={op['recall']:.3f} {pg_total:.2f} ms/q vs Qdrant "
        f"{qd40['client_ms']:.2f} ms/q (hnsw_ef={qd40['hnsw_ef']}, "
        f"recall={qd40['recall']:.3f})"
    )
    print(hdr)
    print("  " + "-" * len(hdr))
    t_buf_used = m["t_buf_us_used"]
    buf_ms = op["buffers"] * t_buf_used / 1e3
    dst_ms = m["distances_est"] * s1["ns_per_distance_sql_fmgr"] / 1e6
    fch_ms = op["fetched"] * s2["us_per_tuple"] / 1e3
    proto = pg_total - op["exec_ms"]
    for k, v in [
        (
            f"buffer manager ({op['buffers']:.0f} accesses x {t_buf_used:.2f} us)",
            buf_ms,
        ),
        (
            f"distance eval fmgr ({m['distances_est']:.0f} x "
            f"{s1['ns_per_distance_sql_fmgr'] / 1e3:.2f} us)",
            dst_ms,
        ),
        (f"heap fetch/visibility ({op['fetched']:.0f} rows)", fch_ms),
        ("scan bookkeeping residual (hash/heap/palloc)", m["residual_ms"]),
        ("parse/plan/protocol (client - exec)", proto),
    ]:
        print(f"  {k:<38s} {v:9.2f} {100 * v / pg_total:13.0f}%")
    print(f"  {'TOTAL (client)':<38s} {pg_total:9.2f}")
    print(
        f"\n  unit costs: PG {s2['us_per_buffer_access']:.2f} us/buffer-access | "
        f"PG dist {s1['ns_per_distance_sql_fmgr']:.0f} ns (fmgr) vs numpy "
        f"{s1['ns_per_distance_numpy_simd']:.0f} ns | Qdrant "
        f"{s5[40]['us_per_ef_unit']:.2f} us/hnsw_ef-unit (th=40), "
        f"{s5[1]['us_per_ef_unit']:.2f} (th=1)"
    )


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    setup_pg(conn)
    s1 = s1_distance(conn)
    s2 = s2_page(conn)
    s3 = s3_bitmap_knn(conn, s1)
    s4 = s4_acorn(conn, s1, s2)
    s5 = s5_qdrant()
    print_ledger(s1, s2, s3, s4, s5)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
    conn.close()

    def default(o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        raise TypeError(type(o))

    with open(OUT, "w") as f:
        json.dump(results, f, indent=1, default=default)
    print(f"\nwrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
