"""Integrated head-to-head: pg_acorn (payload_edges + member_first + scan
fast paths) vs GENUINELY INDEXED Qdrant v1.16.

Modeled on bench/validate_payload_edges.py (PG protocol, refined corr=high
bucket spread so bucket < k yields ~k% selectivity) and
bench/qdrant_codepath_probe.py (Qdrant indexing mechanics, telemetry path
verification).

Protocol hygiene (mandatory, learned the hard way):
  - MEDIAN latency (+p90) per query, never sum-based QPS (fat docker tail).
  - PG plans are verified to show `Index Cond` for the bucket qual and the
    acorn index as the plan node; the plan line is recorded per op point.
  - Buffers/query recorded from EXPLAIN (ANALYZE, BUFFERS) samples.
  - Selectivity constants are INLINED in SQL: a bound int param arrives as
    smallint and the cross-type qual would silently skip index-qual pushdown.
  - Qdrant: optimizers_config indexing_threshold=1000 (KB) forces HNSW
    indexing at n=60K; payload index on bucket is created BEFORE upsert;
    we POLL until indexed_vectors_count ~= points_count and RECORD both.
    Telemetry path counters (filtered_plain / filtered_small_cardinality /
    filtered_large_cardinality / ...) are recorded per op point so the code
    path actually measured is in the results, not assumed.

Engines / configs:
  pg_g1_pe_mf    acorn_gamma=1, payload_edges=on,  member_first=on
  pg_g2_pe_mf    acorn_gamma=2, payload_edges=on,  member_first=on
  pg_g2_nope_mf  acorn_gamma=2, payload_edges=off, member_first=on
      (all with W2 fast-path GUCs ON, prefetch OFF; ef in [40,100,200,400])
  pg_prefilter_exact   btree bitmap prefilter + exact sort (recall 1.0 ref)
  qdrant_default       indexed, default full_scan_threshold (its planner
                       answers low-cardinality filters with an exact scan)
  qdrant_forced        indexed, full_scan_threshold=10 KB (graph forced)
      (hnsw_ef in [40,100,200,400])
  qdrant_exact         params {"exact": true} reference (recall 1.0 ref)

Run (from the integration worktree):
  docker compose -f docker/docker-compose.yml --profile bench run --rm \
      --no-deps bench python3 -u bench/head2head.py
"""

import json
import os
import re
import time

import httpx
import numpy as np
import psycopg

DSN  = os.environ.get("PG_DSN", "postgresql://postgres:postgres@postgres/bench")
QURL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OUT  = os.environ.get("H2H_OUT", "bench/results_head2head.json")

N, DIM, NQ, K = 60_000, 128, 40, 10
EFS   = [40, 100, 200, 400]
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

TELEMETRY_KEYS = (
    "unfiltered_plain", "filtered_plain", "unfiltered_hnsw",
    "filtered_small_cardinality", "filtered_large_cardinality",
    "filtered_exact", "unfiltered_exact",
)

results = {
    "meta": {
        "n": N, "dim": DIM, "nq": NQ, "k": K, "efs": EFS, "sels": SELS,
        "metric": "cosine", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pg_builds": {}, "qdrant_collections": {}, "plan_checks": {},
    },
    "ops": [],
}


def dump():
    with open(OUT, "w") as f:
        json.dump(results, f, indent=1)


def add_op(**kw):
    results["ops"].append(kw)
    dump()


# ---------------------------------------------------------------- fixture
def make_fixture(corr):
    """Vectors + buckets (synthetic.py correlation modes; corr=high uses the
    refined within-block spread from validate_payload_edges.py so that
    bucket < k yields ~k% selectivity) + NQ fresh unit query vectors."""
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
def pg_explain(cur, sel, q, analyze=False):
    kind = "(ANALYZE, BUFFERS, COSTS OFF)" if analyze else "(COSTS OFF)"
    cur.execute(
        f"EXPLAIN {kind} SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
        f"ORDER BY embedding <=> '{qstr(q)}'::vector LIMIT {K}")
    return "\n".join(r[0] for r in cur.fetchall())


def pg_buffers(plan_text):
    """Top-node Buffers line = totals incl. children (first match)."""
    m = re.search(r"Buffers: shared hit=(\d+)(?: read=(\d+))?", plan_text)
    if not m:
        return None
    return int(m.group(1)) + int(m.group(2) or 0)


def pg_measure(cur, queries, truths, sel):
    lats, recalls = [], []
    for _ in range(NWARM):
        cur.execute(
            f"SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
            "ORDER BY embedding <=> %s::vector LIMIT %s", (qstr(queries[0]), K))
        cur.fetchall()
    for q, truth in zip(queries, truths):
        t0 = time.perf_counter()
        cur.execute(
            f"SELECT id FROM h2h_items WHERE bucket < {int(sel)} "
            "ORDER BY embedding <=> %s::vector LIMIT %s", (qstr(q), K))
        ids = {r[0] for r in cur.fetchall()}
        lats.append((time.perf_counter() - t0) * 1000.0)
        recalls.append(len(ids & truth) / K)
    return lats, recalls


def run_pg(corr, vecs, buckets, queries, truths):
    conn = psycopg.connect(DSN, autocommit=True)
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

    # --- reference: prefilter exact (bitmap path), no ANN index present ---
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        for sel in SELS:
            plan = pg_explain(cur, sel, queries[0])
            node = plan.splitlines()[0].strip()
            assert "h2h_acorn_idx" not in plan
            lats, recalls = pg_measure(cur, queries, truths[sel], sel)
            rec, med, p90 = summarize(lats, recalls)
            add_op(engine="pg_prefilter_exact", corr=corr, sel=sel, ef=None,
                   recall=rec, med_ms=med, p90_ms=p90, plan_node=node,
                   lats_ms=[round(x, 3) for x in lats])
            print(f"[pg {corr}] prefilter_exact sel={sel:>2}% recall={rec:.3f} "
                  f"med={med:.2f}ms p90={p90:.2f}ms  [{node[:60]}]", flush=True)

    # --- acorn configs ---
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
            # force the acorn index; W2 fast paths ON (defaults), prefetch OFF
            cur.execute("SET enable_seqscan = off")
            cur.execute("SET enable_bitmapscan = off")
            cur.execute("SET pg_acorn.member_first = on")
            cur.execute("SET pg_acorn.scan_direct_dist = on")
            cur.execute("SET pg_acorn.scan_single_read = on")
            cur.execute("SET pg_acorn.scan_visited_oneprobe = on")
            cur.execute("SET pg_acorn.scan_direct_filter = on")
            cur.execute("SET pg_acorn.scan_prefetch = off")
            for sel in SELS:
                plan = pg_explain(cur, sel, queries[0])
                ok_idx = "Index Scan using h2h_acorn_idx" in plan
                ok_cond = re.search(r"Index Cond: \(bucket < \d+\)", plan)
                results["meta"]["plan_checks"][f"{corr}/{name}/sel{sel}"] = {
                    "index_scan": ok_idx, "index_cond": bool(ok_cond)}
                dump()
                if not (ok_idx and ok_cond):
                    print(f"[pg {corr}] PLAN CHECK FAILED {name} sel={sel}:\n"
                          f"{plan}", flush=True)
                    raise SystemExit(1)
                for ef in EFS:
                    cur.execute(f"SET pg_acorn.ef_search = {ef}")
                    lats, recalls = pg_measure(cur, queries, truths[sel], sel)
                    rec, med, p90 = summarize(lats, recalls)
                    bufs = []
                    for q in queries[:NBUF_SAMPLES]:
                        b = pg_buffers(pg_explain(cur, sel, q, analyze=True))
                        if b is not None:
                            bufs.append(b)
                    add_op(engine=name, corr=corr, sel=sel, ef=ef,
                           recall=rec, med_ms=med, p90_ms=p90,
                           buffers_per_query=(round(float(np.mean(bufs)), 1)
                                              if bufs else None),
                           plan_node="Index Scan using h2h_acorn_idx",
                           lats_ms=[round(x, 3) for x in lats])
                    print(f"[pg {corr}] {name:13s} sel={sel:>2}% ef={ef:>4} "
                          f"recall={rec:.3f} med={med:.2f}ms p90={p90:.2f}ms "
                          f"bufs={bufs}", flush=True)
    conn.close()


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
        # KB; 60K pts / default segments ≫ 1000 KB -> every segment indexed
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


def q_measure(name, queries, truths, sel, params):
    lats, recalls = [], []
    body0 = {"vector": queries[0].tolist(), "limit": K,
             "filter": {"must": [{"key": "bucket", "range": {"lt": sel}}]}}
    if params:
        body0["params"] = params
    for _ in range(NWARM):
        qc.post(f"/collections/{name}/points/search", json=body0)
    pre = telemetry_counts()
    for q, truth in zip(queries, truths):
        body = {"vector": q.tolist(), "limit": K,
                "filter": {"must": [{"key": "bucket", "range": {"lt": sel}}]}}
        if params:
            body["params"] = params
        t0 = time.perf_counter()
        resp = qc.post(f"/collections/{name}/points/search", json=body)
        lats.append((time.perf_counter() - t0) * 1000.0)
        resp.raise_for_status()
        ids = {h["id"] for h in resp.json()["result"]}
        recalls.append(len(ids & truth) / K)
    post = telemetry_counts()
    delta = {k: post[k] - pre[k] for k in TELEMETRY_KEYS if post[k] - pre[k]}
    return lats, recalls, delta


def run_qdrant(corr, vecs, buckets, queries, truths):
    for variant, extra in [("default", None), ("forced", {"full_scan_threshold": 10})]:
        name = f"h2h_{corr}_{variant}"
        print(f"[qdrant {corr}] creating {name} (hnsw_extra={extra})", flush=True)
        q_create(name, extra)
        q_upsert(name, vecs, buckets)
        final = q_poll_indexed(name)
        results["meta"]["qdrant_collections"][name] = final
        dump()
        engine = f"qdrant_{variant}"
        for sel in SELS:
            for ef in EFS:
                lats, recalls, paths = q_measure(name, queries, truths[sel],
                                                 sel, {"hnsw_ef": ef})
                rec, med, p90 = summarize(lats, recalls)
                add_op(engine=engine, corr=corr, sel=sel, ef=ef,
                       recall=rec, med_ms=med, p90_ms=p90,
                       telemetry_paths=paths,
                       indexed_vectors_count=final["indexed_vectors_count"],
                       points_count=final["points_count"],
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
                       lats_ms=[round(x, 3) for x in lats])
                print(f"[qdrant {corr}] qdrant_exact   sel={sel:>2}% "
                      f"recall={rec:.3f} med={med:.2f}ms p90={p90:.2f}ms "
                      f"paths={paths}", flush=True)


# ---------------------------------------------------------------- report
ENGINE_ORDER = ["pg_g1_pe_mf", "pg_g2_pe_mf", "pg_g2_nope_mf",
                "qdrant_default", "qdrant_forced",
                "qdrant_exact", "pg_prefilter_exact"]


def report():
    print("\n" + "=" * 78)
    print("HEAD-TO-HEAD: best operating point reaching recall >= 0.95")
    print("(median ms @ ef; 'max recall' shown when 0.95 never reached)")
    print("=" * 78)
    for corr in CORRS:
        for sel in SELS:
            print(f"\n--- corr={corr}  selectivity={sel}% ---")
            print(f"{'engine/config':<20} {'best op (recall>=0.95)':<34} note")
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
                    print(f"{eng:<20} med={b['med_ms']:7.2f}ms "
                          f"p90={b['p90_ms']:7.2f}ms {efs:<8} "
                          f"recall={b['recall']:.3f}")
                else:
                    b = max(ops, key=lambda o: o["recall"])
                    print(f"{eng:<20} {'never reaches 0.95':<34} "
                          f"max recall={b['recall']:.3f} "
                          f"(med={b['med_ms']:.2f}ms @ef={b['ef']})")


def main():
    for corr in CORRS:
        print(f"\n######## correlation={corr} ########", flush=True)
        vecs, buckets, queries = make_fixture(corr)
        for sel in SELS:
            cnt = int((buckets < sel).sum())
            print(f"[fixture {corr}] sel={sel}% -> {cnt} rows "
                  f"({100.0 * cnt / N:.2f}%)", flush=True)
        truths = {sel: [exact_truth(vecs, buckets, q, sel) for q in queries]
                  for sel in SELS}
        run_pg(corr, vecs, buckets, queries, truths)
        run_qdrant(corr, vecs, buckets, queries, truths)
    results["meta"]["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime())
    dump()
    report()
    print(f"\nresults written to {OUT}", flush=True)


if __name__ == "__main__":
    main()
