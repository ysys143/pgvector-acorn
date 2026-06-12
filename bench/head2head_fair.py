"""EQUALIZED head-to-head: pg_acorn vs genuinely-indexed Qdrant v1.16.

The original bench/head2head.py run was resource-unfair:
  PG:     shared_buffers=128MB, work_mem=4MB, jit=on (per-query literal
          replanning paid the JIT tax), single-threaded AM scans.
  Qdrant: max_search_threads=auto (= 8 CPUs) searching 4 segments in
          parallel, fully memory-mapped.

This script runs two equalized modes:

FAIR-1T (single-thread vs single-thread, both hot):
  PG server (set via docker compose command flags, verified with SHOW):
    shared_buffers=2GB, work_mem=64MB, jit=off,
    max_parallel_workers_per_gather=0, effective_io_concurrency=0,
    track_io_timing=on
  Qdrant service: QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS=1
    (segments are searched serially; segment count recorded).
  Both engines get a full unmeasured pre-warm pass per query set.
  PG queries use bound int4 params + prepared statements
  (prepare_threshold=0); EXPLAIN is verified to show Index Cond with the
  param. Falls back to inlined literals if the param plan degrades.
  PG ef sweep extended to [40,100,200,400,800,1600] (the prior 400 cap left
  g2 still climbing). Qdrant hnsw_ef in [40,100,200,400].

FAIR-NT (both unrestricted, prefilter/exact cells only):
  PG: session SET max_parallel_workers_per_gather=4, planner free
      (graph cells don't parallelize in PG, so only prefilter/exact run).
  Qdrant: QDRANT_MAX_SEARCH_THREADS=0 (auto) — recreate the qdrant service
      before running this mode. Collections from the FAIR-1T run are reused
      (fixture is seeded/deterministic); exact=true reference per cell.

Protocol hygiene carried over from head2head.py:
  - MEDIAN latency (+p90), never sum-based QPS.
  - PG plans verified per op point (Index Cond + acorn index node).
  - Buffers/query from EXPLAIN (ANALYZE, BUFFERS) samples (hit/read split
    recorded so "hot" is proven, not assumed).
  - Qdrant indexing_threshold=1000 KB forces HNSW indexing; payload index
    before upsert; poll until indexed_vectors_count ~= points_count;
    telemetry path counters recorded per op point.

Run (from the integration worktree):
  # FAIR-1T (qdrant must be running with MAX_SEARCH_THREADS=1)
  docker compose -f docker/docker-compose.yml --profile bench run --rm \
      --no-deps bench python3 -u bench/head2head_fair.py --mode fair1t
  # FAIR-NT (recreate qdrant with QDRANT_MAX_SEARCH_THREADS=0 first)
  docker compose -f docker/docker-compose.yml --profile bench run --rm \
      --no-deps bench python3 -u bench/head2head_fair.py --mode fairnt
"""

import argparse
import json
import os
import re
import time

import httpx
import numpy as np
import psycopg

DSN  = os.environ.get("PG_DSN", "postgresql://postgres:postgres@postgres/bench")
QURL = os.environ.get("QDRANT_URL", "http://qdrant:6333")

N, DIM, NQ, K = 60_000, 128, 40, 10
EFS_PG = [40, 100, 200, 400, 800, 1600]
EFS_QD = [40, 100, 200, 400]
SELS  = [1, 10, 40]
CORRS = ["high", "low"]
UPSERT_BATCH = 2000
NBUF_SAMPLES = 3      # EXPLAIN (ANALYZE, BUFFERS) samples per op point
NWARM = 2             # unmeasured warmup queries per op point

PG_CONFIGS = [
    ("pg_g1_pe_mf",   1, "true"),
    ("pg_g2_pe_mf",   2, "true"),
    ("pg_g2_nope_mf", 2, "false"),
]

PG_VERIFY_SETTINGS = [
    "shared_buffers", "work_mem", "jit", "max_parallel_workers_per_gather",
    "effective_io_concurrency", "track_io_timing", "server_version",
]
PG_EXPECT_1T = {
    "shared_buffers": "2GB", "work_mem": "64MB", "jit": "off",
    "max_parallel_workers_per_gather": "0",
    "effective_io_concurrency": "0", "track_io_timing": "on",
}

TELEMETRY_KEYS = (
    "unfiltered_plain", "filtered_plain", "unfiltered_hnsw",
    "filtered_small_cardinality", "filtered_large_cardinality",
    "filtered_exact", "unfiltered_exact",
)

SQL = ("SELECT id FROM h2h_items WHERE bucket < %s::int4 "
       "ORDER BY embedding <=> %s::vector LIMIT %s")

results = {"meta": {}, "ops": []}
OUT = None


def dump():
    with open(OUT, "w") as f:
        json.dump(results, f, indent=1)


def add_op(**kw):
    results["ops"].append(kw)
    dump()


# ---------------------------------------------------------------- fixture
def make_fixture(corr):
    """Identical (seeded) fixture to head2head.py: vectors + buckets + NQ
    fresh unit query vectors. Determinism is what allows FAIR-NT to reuse
    the Qdrant collections built during FAIR-1T."""
    rng = np.random.default_rng(0)
    raw = rng.standard_normal((N, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    if corr == "low":
        buckets = rng.integers(0, 100, size=N)
    else:
        blocks = DIM // 10
        block_norms = np.array([
            np.linalg.norm(vecs[:, i * 10:(i + 1) * 10], axis=1)
            for i in range(blocks)
        ]).T
        dominant = np.argmax(block_norms, axis=1)
        span = 100 // blocks
        buckets = dominant * span + rng.integers(0, span, size=N)
        buckets = np.clip(buckets, 0, 99)
    buckets = buckets.astype(int)
    qraw = rng.standard_normal((NQ, DIM)).astype(np.float32)
    queries = qraw / np.linalg.norm(qraw, axis=1, keepdims=True)
    return vecs, buckets, queries


def exact_truth(vecs, buckets, q, thresh):
    idx = np.where(buckets < thresh)[0]
    sims = vecs[idx] @ q
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())          # serial / point ids start at 1


def qstr(q):
    return "[" + ",".join(f"{x:.6f}" for x in q) + "]"


def summarize(lats, recalls):
    return (float(np.mean(recalls)), float(np.median(lats)),
            float(np.percentile(lats, 90)))


# ---------------------------------------------------------------- postgres
def pg_show(cur, names):
    out = {}
    for n in names:
        cur.execute(f"SHOW {n}")
        out[n] = cur.fetchone()[0]
    return out


def pg_verify_server(conn, mode):
    with conn.cursor() as cur:
        shown = pg_show(cur, PG_VERIFY_SETTINGS)
    results["meta"]["pg_settings"] = shown
    dump()
    print(f"[pg] server settings: {shown}", flush=True)
    if mode == "fair1t":
        bad = {k: (shown[k], v) for k, v in PG_EXPECT_1T.items()
               if shown[k] != v}
        if bad:
            raise SystemExit(f"PG server not in FAIR-1T config: {bad}")
    # extension sanity: merged .so must expose the integrated GUCs
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_settings "
                    "WHERE name LIKE 'pg_acorn.%'")
        ngucs = cur.fetchone()[0]
        cur.execute("SHOW pg_acorn.member_first")
        mf = cur.fetchone()[0]
    results["meta"]["pg_acorn_gucs"] = int(ngucs)
    if ngucs < 5:
        raise SystemExit(f"pg_acorn GUCs missing (n={ngucs}) — wrong .so?")
    print(f"[pg] pg_acorn GUCs: {ngucs} (member_first default={mf})",
          flush=True)


def pg_explain(cur, sel, q, analyze=False, literal=False):
    kind = "(ANALYZE, BUFFERS, COSTS OFF)" if analyze else "(COSTS OFF)"
    if literal:
        cur.execute(
            f"EXPLAIN {kind} SELECT id FROM h2h_items "
            f"WHERE bucket < {int(sel)} "
            f"ORDER BY embedding <=> '{qstr(q)}'::vector LIMIT {K}")
    else:
        cur.execute(f"EXPLAIN {kind} {SQL}", (int(sel), qstr(q), K))
    return "\n".join(r[0] for r in cur.fetchall())


def pg_buffers(plan_text):
    """Top-node Buffers line = totals incl. children (first match).
    Returns (total, hit, read)."""
    m = re.search(r"Buffers: shared hit=(\d+)(?: read=(\d+))?", plan_text)
    if not m:
        return None
    hit, read = int(m.group(1)), int(m.group(2) or 0)
    return hit + read, hit, read


def pg_prewarm(cur, queries, sel, literal=False):
    """Full unmeasured pass over the query set (hot buffer cache)."""
    for q in queries:
        if literal:
            cur.execute(
                f"SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
                f"ORDER BY embedding <=> '{qstr(q)}'::vector LIMIT {K}")
        else:
            cur.execute(SQL, (int(sel), qstr(q), K))
        cur.fetchall()


def pg_measure(cur, queries, truths, sel, literal=False):
    lats, recalls = [], []
    for _ in range(NWARM):
        if literal:
            cur.execute(
                f"SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
                f"ORDER BY embedding <=> '{qstr(queries[0])}'::vector "
                f"LIMIT {K}")
        else:
            cur.execute(SQL, (int(sel), qstr(queries[0]), K))
        cur.fetchall()
    for q, truth in zip(queries, truths):
        t0 = time.perf_counter()
        if literal:
            cur.execute(
                f"SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
                f"ORDER BY embedding <=> '{qstr(q)}'::vector LIMIT {K}")
        else:
            cur.execute(SQL, (int(sel), qstr(q), K))
        ids = {r[0] for r in cur.fetchall()}
        lats.append((time.perf_counter() - t0) * 1000.0)
        recalls.append(len(ids & truth) / K)
    return lats, recalls


def pg_buf_samples(cur, queries, sel, literal=False):
    tot, hit, read = [], [], []
    for q in queries[:NBUF_SAMPLES]:
        b = pg_buffers(pg_explain(cur, sel, q, analyze=True, literal=literal))
        if b is not None:
            tot.append(b[0]); hit.append(b[1]); read.append(b[2])
    if not tot:
        return None, None, None
    return (round(float(np.mean(tot)), 1), round(float(np.mean(hit)), 1),
            round(float(np.mean(read)), 1))


def pg_load(conn, corr, vecs, buckets):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
        cur.execute("DROP TABLE IF EXISTS h2h_items CASCADE")
        cur.execute(f"CREATE TABLE h2h_items (id serial PRIMARY KEY, "
                    f"bucket int, embedding vector({DIM}))")
        print(f"[pg {corr}] loading {N} rows...", flush=True)
        with cur.copy("COPY h2h_items (bucket, embedding) FROM STDIN") as cp:
            for i in range(N):
                cp.write_row((int(buckets[i]), qstr(vecs[i])))
        cur.execute("CREATE INDEX h2h_bucket_btree ON h2h_items (bucket)")
        cur.execute("ANALYZE h2h_items")


def run_pg_prefilter(conn, corr, queries, truths, mode):
    """Exact reference. FAIR-1T: bitmap prefilter forced (seqscan off),
    server-side mpwg=0/jit=off. FAIR-NT: planner free, session mpwg=4."""
    engine = ("pg_prefilter_exact" if mode == "fair1t"
              else "pg_prefilter_exact_nt")
    with conn.cursor() as cur:
        if mode == "fair1t":
            cur.execute("SET enable_seqscan = off")
        else:
            cur.execute("SET max_parallel_workers_per_gather = 4")
        sess = pg_show(cur, ["max_parallel_workers_per_gather", "jit",
                             "work_mem"])
        for sel in SELS:
            plan = pg_explain(cur, sel, queries[0])
            node = plan.splitlines()[0].strip()
            assert "h2h_acorn_idx" not in plan
            workers = re.search(r"Workers Planned: (\d+)", plan)
            pg_prewarm(cur, queries, sel)
            lats, recalls = pg_measure(cur, queries, truths[sel], sel)
            rec, med, p90 = summarize(lats, recalls)
            bt, bh, br = pg_buf_samples(cur, queries, sel)
            add_op(engine=engine, corr=corr, sel=sel, ef=None,
                   recall=rec, med_ms=med, p90_ms=p90, plan_node=node,
                   workers_planned=(int(workers.group(1)) if workers else 0),
                   session_settings=sess,
                   buffers_per_query=bt, buffers_hit=bh, buffers_read=br,
                   lats_ms=[round(x, 3) for x in lats])
            print(f"[pg {corr}] {engine} sel={sel:>2}% recall={rec:.3f} "
                  f"med={med:.2f}ms p90={p90:.2f}ms bufs={bt} (hit={bh} "
                  f"read={br}) [{node[:60]}]", flush=True)


def run_pg_acorn(conn, corr, queries, truths):
    for name, gamma, pe in PG_CONFIGS:
        with conn.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS h2h_acorn_idx")
            print(f"[pg {corr}] building {name} (gamma={gamma}, "
                  f"payload_edges={pe})...", flush=True)
            t0 = time.perf_counter()
            cur.execute(f"""CREATE INDEX h2h_acorn_idx ON h2h_items
                            USING acorn_hnsw (embedding vector_cosine_ops,
                                              bucket int4_acorn_ops)
                            WITH (m = 16, ef_construction = 64,
                                  acorn_gamma = {gamma},
                                  acorn_payload_edges = {pe})""")
            bt = time.perf_counter() - t0
            results["meta"]["pg_builds"][f"{corr}/{name}"] = round(bt, 1)
            dump()
            print(f"[pg {corr}]   built in {bt:.1f}s", flush=True)

        with conn.cursor() as cur:
            # force the acorn index; W2 fast paths ON, prefetch OFF
            cur.execute("SET enable_seqscan = off")
            cur.execute("SET enable_bitmapscan = off")
            cur.execute("SET pg_acorn.member_first = on")
            cur.execute("SET pg_acorn.scan_direct_dist = on")
            cur.execute("SET pg_acorn.scan_single_read = on")
            cur.execute("SET pg_acorn.scan_visited_oneprobe = on")
            cur.execute("SET pg_acorn.scan_direct_filter = on")
            cur.execute("SET pg_acorn.scan_prefetch = off")
            for sel in SELS:
                # plan check with BOUND int4 param (cross-type opclass fix)
                literal = False
                plan = pg_explain(cur, sel, queries[0])
                ok_idx = "Index Scan using h2h_acorn_idx" in plan
                ok_cond = re.search(r"Index Cond: \(bucket <", plan)
                if not (ok_idx and ok_cond):
                    print(f"[pg {corr}] param plan degraded for {name} "
                          f"sel={sel}, falling back to literals:\n{plan}",
                          flush=True)
                    literal = True
                    plan = pg_explain(cur, sel, queries[0], literal=True)
                    ok_idx = "Index Scan using h2h_acorn_idx" in plan
                    ok_cond = re.search(r"Index Cond: \(bucket <", plan)
                results["meta"]["plan_checks"][f"{corr}/{name}/sel{sel}"] = {
                    "index_scan": ok_idx, "index_cond": bool(ok_cond),
                    "bound_params": not literal,
                    "cond_line": next((ln.strip() for ln in plan.splitlines()
                                       if "Index Cond" in ln), None)}
                dump()
                if not (ok_idx and ok_cond):
                    print(f"[pg {corr}] PLAN CHECK FAILED {name} sel={sel}:\n"
                          f"{plan}", flush=True)
                    raise SystemExit(1)
                # full pre-warm pass at the largest ef (hot buffer cache)
                cur.execute(f"SET pg_acorn.ef_search = {EFS_PG[-1]}")
                pg_prewarm(cur, queries, sel, literal=literal)
                for ef in EFS_PG:
                    cur.execute(f"SET pg_acorn.ef_search = {ef}")
                    lats, recalls = pg_measure(cur, queries, truths[sel],
                                               sel, literal=literal)
                    rec, med, p90 = summarize(lats, recalls)
                    bt, bh, br = pg_buf_samples(cur, queries, sel,
                                                literal=literal)
                    add_op(engine=name, corr=corr, sel=sel, ef=ef,
                           recall=rec, med_ms=med, p90_ms=p90,
                           buffers_per_query=bt, buffers_hit=bh,
                           buffers_read=br, bound_params=not literal,
                           plan_node="Index Scan using h2h_acorn_idx",
                           lats_ms=[round(x, 3) for x in lats])
                    print(f"[pg {corr}] {name:13s} sel={sel:>2}% ef={ef:>4} "
                          f"recall={rec:.3f} med={med:.2f}ms p90={p90:.2f}ms "
                          f"bufs={bt} (hit={bh} read={br})", flush=True)


# ---------------------------------------------------------------- qdrant
qc = httpx.Client(base_url=QURL, timeout=600.0)


def telemetry_counts():
    r = qc.get("/telemetry", params={"details_level": 10}).json()["result"]
    tot = dict.fromkeys(TELEMETRY_KEYS, 0)

    def walk(o):
        if isinstance(o, dict):
            if "index_name" in o:
                for k in TELEMETRY_KEYS:
                    v = o.get(k) or {}
                    if isinstance(v, dict):
                        tot[k] += int(v.get("count") or 0)
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(r)
    return tot


def telemetry_find_threads():
    """Best-effort: pull any max_search_threads value out of /telemetry."""
    r = qc.get("/telemetry", params={"details_level": 10}).json()["result"]
    found = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "max_search_threads":
                    found.append({"path": path + "/" + k, "value": v})
                else:
                    walk(v, path + "/" + k)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(r, "")
    return found


def q_info(name):
    r = qc.get(f"/collections/{name}").json()["result"]
    return {"status": r["status"],
            "points_count": r["points_count"],
            "indexed_vectors_count": r["indexed_vectors_count"],
            "segments_count": r["segments_count"]}


def q_create(name, hnsw_extra):
    qc.delete(f"/collections/{name}")
    time.sleep(1)
    qc.put(f"/collections/{name}", json={
        "vectors": {"size": DIM, "distance": "Cosine"},
        "hnsw_config": {"m": 16, "ef_construct": 64, **(hnsw_extra or {})},
        # KB; 60K pts / default segments >> 1000 KB -> every segment indexed
        "optimizers_config": {"indexing_threshold": 1000},
    }).raise_for_status()
    # payload index BEFORE upsert
    qc.put(f"/collections/{name}/index",
           json={"field_name": "bucket", "field_schema": "integer"},
           ).raise_for_status()


def q_upsert(name, vecs, buckets):
    for s in range(0, N, UPSERT_BATCH):
        e = min(s + UPSERT_BATCH, N)
        pts = [{"id": i + 1, "vector": vecs[i].tolist(),
                "payload": {"bucket": int(buckets[i])}}
               for i in range(s, e)]
        qc.put(f"/collections/{name}/points" + ("?wait=true" if e >= N else ""),
               json={"points": pts}).raise_for_status()


def q_poll_indexed(name, settle_s=20, timeout_s=600):
    t0 = time.time()
    last, stable_since = None, time.time()
    while time.time() - t0 < timeout_s:
        i = q_info(name)
        key = (i["status"], i["indexed_vectors_count"])
        if key != last:
            print(f"[qdrant] {name} t={time.time()-t0:5.1f}s {i}", flush=True)
            last, stable_since = key, time.time()
        if (i["status"] == "green"
                and i["indexed_vectors_count"] >= 0.95 * i["points_count"]
                and time.time() - stable_since >= settle_s):
            break
        time.sleep(2)
    final = q_info(name)
    print(f"[qdrant] {name} settled: {final}", flush=True)
    if final["indexed_vectors_count"] < 0.95 * final["points_count"]:
        print(f"[qdrant] WARNING: {name} NOT genuinely indexed "
              f"({final['indexed_vectors_count']}/{final['points_count']})",
              flush=True)
    return final


def q_ensure(name, hnsw_extra, vecs, buckets, reuse=True):
    """Reuse an existing fully-indexed collection (deterministic fixture)
    or (re)create it."""
    if reuse:
        try:
            i = q_info(name)
            if (i["points_count"] == N
                    and i["indexed_vectors_count"] >= 0.95 * N):
                print(f"[qdrant] reusing {name}: {i}", flush=True)
                return i
        except Exception:
            pass
    q_create(name, hnsw_extra)
    q_upsert(name, vecs, buckets)
    return q_poll_indexed(name)


def q_search_body(q, sel, params):
    body = {"vector": q.tolist(), "limit": K,
            "filter": {"must": [{"key": "bucket", "range": {"lt": sel}}]}}
    if params:
        body["params"] = params
    return body


def q_prewarm(name, queries, sel, params):
    for q in queries:
        qc.post(f"/collections/{name}/points/search",
                json=q_search_body(q, sel, params))


def q_measure(name, queries, truths, sel, params):
    lats, recalls = [], []
    for _ in range(NWARM):
        qc.post(f"/collections/{name}/points/search",
                json=q_search_body(queries[0], sel, params))
    pre = telemetry_counts()
    for q, truth in zip(queries, truths):
        body = q_search_body(q, sel, params)
        t0 = time.perf_counter()
        resp = qc.post(f"/collections/{name}/points/search", json=body)
        lats.append((time.perf_counter() - t0) * 1000.0)
        resp.raise_for_status()
        ids = {h["id"] for h in resp.json()["result"]}
        recalls.append(len(ids & truth) / K)
    post = telemetry_counts()
    delta = {k: post[k] - pre[k] for k in TELEMETRY_KEYS if post[k] - pre[k]}
    return lats, recalls, delta


def run_qdrant_full(corr, vecs, buckets, queries, truths):
    """FAIR-1T: default + forced-graph variants, ef sweep, exact ref."""
    for variant, extra in [("default", None),
                           ("forced", {"full_scan_threshold": 10})]:
        name = f"h2h_{corr}_{variant}"
        final = q_ensure(name, extra, vecs, buckets, reuse=False)
        results["meta"]["qdrant_collections"][name] = final
        dump()
        engine = f"qdrant_{variant}"
        for sel in SELS:
            q_prewarm(name, queries, sel, {"hnsw_ef": EFS_QD[-1]})
            for ef in EFS_QD:
                lats, recalls, paths = q_measure(name, queries, truths[sel],
                                                 sel, {"hnsw_ef": ef})
                rec, med, p90 = summarize(lats, recalls)
                add_op(engine=engine, corr=corr, sel=sel, ef=ef,
                       recall=rec, med_ms=med, p90_ms=p90,
                       telemetry_paths=paths,
                       indexed_vectors_count=final["indexed_vectors_count"],
                       points_count=final["points_count"],
                       segments_count=final["segments_count"],
                       lats_ms=[round(x, 3) for x in lats])
                print(f"[qdrant {corr}] {engine:14s} sel={sel:>2}% "
                      f"hnsw_ef={ef:>4} recall={rec:.3f} med={med:.2f}ms "
                      f"p90={p90:.2f}ms paths={paths}", flush=True)
            if variant == "forced":          # one exact reference per sel
                lats, recalls, paths = q_measure(name, queries, truths[sel],
                                                 sel, {"exact": True})
                rec, med, p90 = summarize(lats, recalls)
                add_op(engine="qdrant_exact", corr=corr, sel=sel, ef=None,
                       recall=rec, med_ms=med, p90_ms=p90,
                       telemetry_paths=paths,
                       indexed_vectors_count=final["indexed_vectors_count"],
                       points_count=final["points_count"],
                       segments_count=final["segments_count"],
                       lats_ms=[round(x, 3) for x in lats])
                print(f"[qdrant {corr}] qdrant_exact   sel={sel:>2}% "
                      f"recall={rec:.3f} med={med:.2f}ms p90={p90:.2f}ms "
                      f"paths={paths}", flush=True)


def run_qdrant_exact_nt(corr, vecs, buckets, queries, truths):
    """FAIR-NT: exact=true reference only, threads unrestricted."""
    name = f"h2h_{corr}_default"
    final = q_ensure(name, None, vecs, buckets, reuse=True)
    results["meta"]["qdrant_collections"][name] = final
    dump()
    for sel in SELS:
        q_prewarm(name, queries, sel, {"exact": True})
        lats, recalls, paths = q_measure(name, queries, truths[sel],
                                         sel, {"exact": True})
        rec, med, p90 = summarize(lats, recalls)
        add_op(engine="qdrant_exact_nt", corr=corr, sel=sel, ef=None,
               recall=rec, med_ms=med, p90_ms=p90, telemetry_paths=paths,
               indexed_vectors_count=final["indexed_vectors_count"],
               points_count=final["points_count"],
               segments_count=final["segments_count"],
               lats_ms=[round(x, 3) for x in lats])
        print(f"[qdrant {corr}] qdrant_exact_nt sel={sel:>2}% "
              f"recall={rec:.3f} med={med:.2f}ms p90={p90:.2f}ms "
              f"paths={paths}", flush=True)


# ---------------------------------------------------------------- report
ENGINE_ORDER = ["pg_g1_pe_mf", "pg_g2_pe_mf", "pg_g2_nope_mf",
                "qdrant_default", "qdrant_forced",
                "qdrant_exact", "pg_prefilter_exact",
                "qdrant_exact_nt", "pg_prefilter_exact_nt"]


def report():
    print("\n" + "=" * 78)
    print("FAIR HEAD-TO-HEAD: best operating point reaching recall >= 0.95")
    print("(median ms @ ef; 'max recall' shown when 0.95 never reached)")
    print("=" * 78)
    for corr in CORRS:
        for sel in SELS:
            print(f"\n--- corr={corr}  selectivity={sel}% ---")
            print(f"{'engine/config':<22} {'best op (recall>=0.95)':<34} note")
            for eng in ENGINE_ORDER:
                ops = [o for o in results["ops"]
                       if o["engine"] == eng and o["corr"] == corr
                       and o["sel"] == sel]
                if not ops:
                    continue
                ok = [o for o in ops if o["recall"] >= 0.95]
                if ok:
                    b = min(ok, key=lambda o: o["med_ms"])
                    efs = f"ef={b['ef']}" if b["ef"] else "exact"
                    print(f"{eng:<22} med={b['med_ms']:7.2f}ms "
                          f"p90={b['p90_ms']:7.2f}ms {efs:<8} "
                          f"recall={b['recall']:.3f}")
                else:
                    b = max(ops, key=lambda o: o["recall"])
                    print(f"{eng:<22} {'never reaches 0.95':<34} "
                          f"max recall={b['recall']:.3f} "
                          f"(med={b['med_ms']:.2f}ms @ef={b['ef']})")


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["fair1t", "fairnt"], default="fair1t")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    OUT = args.out or os.environ.get(
        "H2H_OUT", f"bench/results_head2head_{args.mode}.json")

    results["meta"].update({
        "mode": args.mode, "n": N, "dim": DIM, "nq": NQ, "k": K,
        "efs_pg": EFS_PG, "efs_qdrant": EFS_QD, "sels": SELS,
        "metric": "cosine",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pg_builds": {}, "qdrant_collections": {}, "plan_checks": {},
        "qdrant_threads_env": os.environ.get("H2H_QDRANT_THREADS_ENV"),
    })
    results["meta"]["qdrant_max_search_threads_telemetry"] = \
        telemetry_find_threads()
    print(f"[qdrant] max_search_threads via telemetry: "
          f"{results['meta']['qdrant_max_search_threads_telemetry']}",
          flush=True)

    # prepare_threshold=0: server-side prepared statements from the first
    # execution (no per-query replanning of the statement text).
    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    pg_verify_server(conn, args.mode)

    for corr in CORRS:
        print(f"\n######## mode={args.mode} correlation={corr} ########",
              flush=True)
        vecs, buckets, queries = make_fixture(corr)
        for sel in SELS:
            cnt = int((buckets < sel).sum())
            print(f"[fixture {corr}] sel={sel}% -> {cnt} rows "
                  f"({100.0 * cnt / N:.2f}%)", flush=True)
        truths = {sel: [exact_truth(vecs, buckets, q, sel) for q in queries]
                  for sel in SELS}
        pg_load(conn, corr, vecs, buckets)
        run_pg_prefilter(conn, corr, queries, truths, args.mode)
        if args.mode == "fair1t":
            run_pg_acorn(conn, corr, queries, truths)
            run_qdrant_full(corr, vecs, buckets, queries, truths)
        else:
            run_qdrant_exact_nt(corr, vecs, buckets, queries, truths)
    conn.close()
    results["meta"]["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime())
    dump()
    report()
    print(f"\nresults written to {OUT}", flush=True)


if __name__ == "__main__":
    main()
