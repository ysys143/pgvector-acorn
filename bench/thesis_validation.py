"""THESIS VALIDATION: pg_acorn vs best PG-NATIVE alternatives at n=250K.

Claim under test: at n=250K, dim=128, cosine, CORRELATED filters,
selectivity 1-10%, pg_acorn in-filter (acorn_hnsw + payload_edges +
member_first) reaches recall>=0.95 at lower median latency than BOTH
  (a) prefilter exact (btree bitmap + sort) and
  (b) pgvector 0.8.0 HNSW postfilter with iterative scan.
Unit-cost math predicts an in-filter window of s in [~0.5%, ~10%].

Engines (each index built once, build time recorded):
  1. pg_prefilter        btree on bucket only, NO vector index, planner free.
                         Plan node recorded; recall must be 1.0.
  2. pgv_post_relaxed /  pgvector hnsw (m=16, ef_construction=64), btree
     pgv_post_strict     ABSENT; hnsw.iterative_scan=relaxed_order /
                         strict_order; hnsw.max_scan_tuples=40000;
                         sweep hnsw.ef_search in [40..1000].
                         Plan verified: hnsw Index Scan + Filter on bucket.
  3. pgv_free            pgvector hnsw + btree both present, planner FREE
                         (competent-user default: iterative_scan=
                         relaxed_order, max_scan_tuples=40000); plan node
                         recorded per cell; ef swept only if hnsw chosen.
  4. acorn_g1_infilter / acorn_hnsw gamma in {1,2}, payload_edges=on,
     acorn_g2_infilter   member_first=on, W2 fastpaths on, prefetch off;
                         ef in [40..1600]. Index Cond pushdown verified.
  5. acorn_g2_postfilter same gamma=2+payload index, member_first=off,
                         qual wrapped as (bucket + 0) < N so the AM cannot
                         take it (plan must show Filter, no Index Cond).

Fair config (FAIR-1T, as bench/head2head_fair.py): single client, server
jit=off work_mem=64MB shared_buffers=2GB mpwg=0 (verified via SHOW),
full unmeasured prewarm pass per op point, MEDIAN+p90 latency, EXPLAIN
(ANALYZE,BUFFERS) samples with plan-node + Index Cond verification,
bound int4 params with prepared statements (prepare_threshold=0).

Results are written incrementally (JSON after every op point). Stages are
resumable:
  --stages load,prefilter,postfilter,free,acorn_g1,acorn_g2
(acorn_g2 includes the postfilter-mode cells). The currently-built acorn
index variant is tracked in table tv_meta so resumes skip rebuilds.

Run (from the integration worktree):
  docker compose -f docker/docker-compose.yml --profile bench run --rm \
      --no-deps bench python3 -u bench/thesis_validation.py
Smoke (small N, fast — validates every plan check end to end):
  ... bench python3 -u bench/thesis_validation.py --smoke
"""

import argparse
import json
import os
import re
import time

import numpy as np
import psycopg

DSN = os.environ.get("PG_DSN", "postgresql://postgres:postgres@postgres/bench")

N, DIM, NQ, K = 250_000, 128, 40, 10
SELS = [1, 2, 5, 10, 20]
EFS_ACORN = [40, 100, 200, 400, 800, 1600]
EFS_PGV = [40, 100, 200, 400, 800, 1000]   # hnsw.ef_search hard cap = 1000
MAX_SCAN_TUPLES = 40_000
NBUF_SAMPLES = 3
NWARM = 2

PG_EXPECT = {
    "shared_buffers": "2GB", "work_mem": "64MB", "jit": "off",
    "max_parallel_workers_per_gather": "0",
}

SQL = ("SELECT id FROM tv_items WHERE bucket < %s::int4 "
       "ORDER BY embedding <=> %s::vector LIMIT %s")
SQL_WRAP = ("SELECT id FROM tv_items WHERE (bucket + 0) < %s::int4 "
            "ORDER BY embedding <=> %s::vector LIMIT %s")

results = {"meta": {}, "ops": []}
OUT = None


def dump():
    with open(OUT, "w") as f:
        json.dump(results, f, indent=1)


def add_op(**kw):
    results["ops"].append(kw)
    dump()


def summarize(lats, recalls):
    return (float(np.mean(recalls)), float(np.median(lats)),
            float(np.percentile(lats, 90)))


# ---------------------------------------------------------------- fixture
def make_fixture():
    """Correlated buckets (refined within-block spread, identical scheme to
    head2head_fair.py corr='high'): bucket is derived from the dominant
    10-dim block of the vector, spread within the block so that bucket<k
    selects ~k% of rows. Fixed seed -> deterministic across resumes."""
    rng = np.random.default_rng(0)
    raw = rng.standard_normal((N, DIM)).astype(np.float32)
    vecs = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    blocks = DIM // 10
    block_norms = np.array([
        np.linalg.norm(vecs[:, i * 10:(i + 1) * 10], axis=1)
        for i in range(blocks)
    ]).T
    dominant = np.argmax(block_norms, axis=1)
    span = 100 // blocks
    buckets = dominant * span + rng.integers(0, span, size=N)
    buckets = np.clip(buckets, 0, 99).astype(int)
    qraw = rng.standard_normal((NQ, DIM)).astype(np.float32)
    queries = qraw / np.linalg.norm(qraw, axis=1, keepdims=True)
    return vecs, buckets, queries


def exact_truth(vecs, buckets, q, thresh):
    idx = np.where(buckets < thresh)[0]
    sims = vecs[idx] @ q
    top = idx[np.argsort(-sims)[:K]]
    return set((top + 1).tolist())            # serial ids start at 1


def qstr(q):
    return "[" + ",".join(f"{x:.6f}" for x in q) + "]"


# ---------------------------------------------------------------- pg utils
def pg_show(cur, names):
    out = {}
    for n in names:
        cur.execute(f"SHOW {n}")
        out[n] = cur.fetchone()[0]
    return out


def pg_verify_server(conn):
    with conn.cursor() as cur:
        shown = pg_show(cur, list(PG_EXPECT) + [
            "effective_io_concurrency", "track_io_timing", "server_version"])
        cur.execute("SELECT count(*) FROM pg_settings "
                    "WHERE name LIKE 'pg_acorn.%'")
        ngucs = cur.fetchone()[0]
        cur.execute("SHOW pg_acorn.member_first")
        mf = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension "
                    "WHERE extname='vector'")
        vver = cur.fetchone()[0]
    bad = {k: (shown[k], v) for k, v in PG_EXPECT.items() if shown[k] != v}
    if bad:
        raise SystemExit(f"PG server not in FAIR config: {bad}")
    if ngucs < 5:
        raise SystemExit(f"pg_acorn GUCs missing (n={ngucs}) — wrong .so?")
    results["meta"]["pg_settings"] = shown
    results["meta"]["pg_acorn_gucs"] = int(ngucs)
    results["meta"]["pgvector_version"] = vver
    dump()
    print(f"[pg] settings={shown} pg_acorn_gucs={ngucs} "
          f"member_first_default={mf} pgvector={vver}", flush=True)


def pg_explain(cur, sql, sel, q, analyze=False):
    kind = "(ANALYZE, BUFFERS, COSTS OFF)" if analyze else "(COSTS OFF)"
    cur.execute(f"EXPLAIN {kind} {sql}", (int(sel), qstr(q), K))
    return "\n".join(r[0] for r in cur.fetchall())


def pg_buffers(plan_text):
    m = re.search(r"Buffers: shared hit=(\d+)(?: read=(\d+))?", plan_text)
    if not m:
        return None
    hit, read = int(m.group(1)), int(m.group(2) or 0)
    return hit + read, hit, read


def pg_prewarm(cur, sql, queries, sel):
    for q in queries:
        cur.execute(sql, (int(sel), qstr(q), K))
        cur.fetchall()


def pg_measure(cur, sql, queries, truths, sel):
    lats, recalls = [], []
    for _ in range(NWARM):
        cur.execute(sql, (int(sel), qstr(queries[0]), K))
        cur.fetchall()
    for q, truth in zip(queries, truths):
        t0 = time.perf_counter()
        cur.execute(sql, (int(sel), qstr(q), K))
        ids = {r[0] for r in cur.fetchall()}
        lats.append((time.perf_counter() - t0) * 1000.0)
        recalls.append(len(ids & truth) / K)
    return lats, recalls


def pg_buf_samples(cur, sql, queries, sel):
    tot, hit, read = [], [], []
    for q in queries[:NBUF_SAMPLES]:
        b = pg_buffers(pg_explain(cur, sql, sel, q, analyze=True))
        if b is not None:
            tot.append(b[0]); hit.append(b[1]); read.append(b[2])
    if not tot:
        return None, None, None
    return (round(float(np.mean(tot)), 1), round(float(np.mean(hit)), 1),
            round(float(np.mean(read)), 1))


def scan_node(plan):
    """First line mentioning a Scan (the actual access path), else line 0."""
    for ln in plan.splitlines():
        if "Scan" in ln:
            return ln.strip().lstrip("-> ")
    return plan.splitlines()[0].strip()


def plan_check(name, plan, must_re=(), must_not_re=()):
    ok = all(re.search(p, plan) for p in must_re) and \
        not any(re.search(p, plan) for p in must_not_re)
    results["meta"].setdefault("plan_checks", {})[name] = {
        "ok": bool(ok),
        "node": scan_node(plan),
        "cond_or_filter": [ln.strip() for ln in plan.splitlines()
                           if "Index Cond" in ln or "Filter" in ln],
    }
    dump()
    if not ok:
        print(f"[PLAN CHECK FAILED] {name}\n{plan}", flush=True)
        raise SystemExit(1)
    return scan_node(plan)


# ---------------------------------------------------------------- indexes
def drop_idx(cur, name):
    cur.execute(f"DROP INDEX IF EXISTS {name}")


def idx_exists(cur, name):
    cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname=%s",
                (name,))
    return cur.fetchone()[0] == 1


def timed_index(cur, key, ddl, idxname):
    cur.execute("SET maintenance_work_mem = '2GB'")
    print(f"[build] {key}: {ddl.strip().splitlines()[0]} ...", flush=True)
    t0 = time.perf_counter()
    cur.execute(ddl)
    bt = time.perf_counter() - t0
    cur.execute(f"SELECT pg_relation_size('{idxname}')")
    size = cur.fetchone()[0]
    results["meta"].setdefault("builds", {})[key] = {
        "seconds": round(bt, 1), "size_mb": round(size / 1048576, 1)}
    dump()
    print(f"[build] {key}: {bt:.1f}s, {size/1048576:.1f} MB", flush=True)
    # flush build dirty pages NOW so checkpointer/bgwriter IO does not
    # contend with the query measurements that follow (rep1 showed 4-10x
    # median inflation in stages measured right after large builds)
    cur.execute("CHECKPOINT")


def meta_get(cur, k):
    cur.execute("CREATE TABLE IF NOT EXISTS tv_meta "
                "(key text PRIMARY KEY, val text)")
    cur.execute("SELECT val FROM tv_meta WHERE key=%s", (k,))
    r = cur.fetchone()
    return r[0] if r else None


def meta_set(cur, k, v):
    cur.execute("CREATE TABLE IF NOT EXISTS tv_meta "
                "(key text PRIMARY KEY, val text)")
    cur.execute("INSERT INTO tv_meta VALUES (%s,%s) ON CONFLICT (key) "
                "DO UPDATE SET val=EXCLUDED.val", (k, v))


def ensure_acorn(cur, variant, gamma):
    """Build tv_acorn_idx for the requested variant unless already built."""
    if meta_get(cur, "acorn_index") == variant and idx_exists(
            cur, "tv_acorn_idx"):
        print(f"[build] reusing acorn index {variant}", flush=True)
        return
    drop_idx(cur, "tv_acorn_idx")
    meta_set(cur, "acorn_index", "building")
    timed_index(
        cur, f"acorn_{variant}",
        f"""CREATE INDEX tv_acorn_idx ON tv_items
            USING acorn_hnsw (embedding vector_cosine_ops,
                              bucket int4_acorn_ops)
            WITH (m = 16, ef_construction = 64, acorn_gamma = {gamma},
                  acorn_payload_edges = true)""",
        "tv_acorn_idx")
    meta_set(cur, "acorn_index", variant)


# ---------------------------------------------------------------- stages
def stage_load(conn, vecs, buckets, force=False):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_acorn")
        cur.execute("SELECT count(*) FROM pg_tables "
                    "WHERE tablename='tv_items'")
        if cur.fetchone()[0] == 1 and not force:
            cur.execute("SELECT count(*) FROM tv_items")
            if cur.fetchone()[0] == N:
                print(f"[load] reusing tv_items ({N} rows)", flush=True)
                return
        cur.execute("DROP TABLE IF EXISTS tv_items CASCADE")
        cur.execute("DROP TABLE IF EXISTS tv_meta CASCADE")
        cur.execute(f"CREATE TABLE tv_items (id serial PRIMARY KEY, "
                    f"bucket int, embedding vector({DIM}))")
        print(f"[load] COPYing {N} rows...", flush=True)
        t0 = time.perf_counter()
        with cur.copy("COPY tv_items (bucket, embedding) FROM STDIN") as cp:
            for i in range(N):
                cp.write_row((int(buckets[i]), qstr(vecs[i])))
        cur.execute("ANALYZE tv_items")
        print(f"[load] done in {time.perf_counter()-t0:.1f}s", flush=True)


def run_cells(cur, engine, sql, queries, truths, efs, set_ef=None,
              extra_fields=None, plan_fn=None):
    """Measure one engine across SELS (x efs when set_ef is given)."""
    for sel in SELS:
        node = plan_fn(sel) if plan_fn else None
        if set_ef:
            set_ef(efs[-1])
        pg_prewarm(cur, sql, queries, sel)
        for ef in (efs if set_ef else [None]):
            if set_ef:
                set_ef(ef)
            lats, recalls = pg_measure(cur, sql, queries, truths[sel], sel)
            rec, med, p90 = summarize(lats, recalls)
            bt, bh, br = pg_buf_samples(cur, sql, queries, sel)
            add_op(engine=engine, sel=sel, ef=ef, recall=rec,
                   med_ms=med, p90_ms=p90, plan_node=node,
                   buffers_per_query=bt, buffers_hit=bh, buffers_read=br,
                   lats_ms=[round(x, 3) for x in lats],
                   **(extra_fields or {}))
            print(f"[{engine:19s}] sel={sel:>2}% ef={str(ef):>4} "
                  f"recall={rec:.3f} med={med:6.2f}ms p90={p90:6.2f}ms "
                  f"bufs={bt} (hit={bh} read={br})", flush=True)


def stage_prefilter(conn, queries, truths):
    """Config 1: btree only, no vector index, planner free. recall==1.0."""
    with conn.cursor() as cur:
        native_session(cur)
        drop_idx(cur, "tv_hnsw_idx")
        meta_set(cur, "hnsw", "absent")
        drop_idx(cur, "tv_acorn_idx")
        meta_set(cur, "acorn_index", "none")
        if meta_get(cur, "btree") != "built" or not idx_exists(
                cur, "tv_bucket_btree"):
            drop_idx(cur, "tv_bucket_btree")
            timed_index(cur, "btree",
                        "CREATE INDEX tv_bucket_btree ON tv_items (bucket)",
                        "tv_bucket_btree")
            meta_set(cur, "btree", "built")
        cur.execute("ANALYZE tv_items")

        def plan_fn(sel):
            plan = pg_explain(cur, SQL, sel, queries[0])
            return plan_check(
                f"pg_prefilter/sel{sel}", plan,
                must_not_re=[r"tv_hnsw_idx", r"tv_acorn_idx"])

        run_cells(cur, "pg_prefilter", SQL, queries, truths, [None],
                  plan_fn=plan_fn)
        for op in results["ops"]:
            if op["engine"] == "pg_prefilter" and op["recall"] < 0.999:
                raise SystemExit(f"prefilter recall != 1.0: {op}")


def stage_postfilter(conn, queries, truths):
    """Config 2: pgvector hnsw, btree ABSENT, iterative scan sweep."""
    with conn.cursor() as cur:
        native_session(cur)
        drop_idx(cur, "tv_bucket_btree")
        meta_set(cur, "btree", "absent")
        drop_idx(cur, "tv_acorn_idx")
        meta_set(cur, "acorn_index", "none")
        if meta_get(cur, "hnsw") != "built" or not idx_exists(
                cur, "tv_hnsw_idx"):
            drop_idx(cur, "tv_hnsw_idx")
            timed_index(cur, "pgvector_hnsw",
                        "CREATE INDEX tv_hnsw_idx ON tv_items USING hnsw "
                        "(embedding vector_cosine_ops) "
                        "WITH (m = 16, ef_construction = 64)",
                        "tv_hnsw_idx")
            meta_set(cur, "hnsw", "built")
        cur.execute("ANALYZE tv_items")
        cur.execute(f"SET hnsw.max_scan_tuples = {MAX_SCAN_TUPLES}")
        for order in ["relaxed_order", "strict_order"]:
            cur.execute(f"SET hnsw.iterative_scan = {order}")
            engine = f"pgv_post_{order.split('_')[0]}"

            def plan_fn(sel):
                plan = pg_explain(cur, SQL, sel, queries[0])
                return plan_check(
                    f"{engine}/sel{sel}", plan,
                    must_re=[r"Index Scan using tv_hnsw_idx",
                             r"Filter: \(bucket <"],
                    must_not_re=[r"Index Cond"])

            run_cells(cur, engine, SQL, queries, truths, EFS_PGV,
                      set_ef=lambda ef: cur.execute(
                          f"SET hnsw.ef_search = {ef}"),
                      extra_fields={"iterative_scan": order,
                                    "max_scan_tuples": MAX_SCAN_TUPLES},
                      plan_fn=plan_fn)


def stage_free(conn, queries, truths):
    """Config 3: hnsw + btree both present, planner free."""
    with conn.cursor() as cur:
        native_session(cur)
        if not idx_exists(cur, "tv_hnsw_idx"):
            raise SystemExit("free stage requires postfilter stage's hnsw")
        if not idx_exists(cur, "tv_bucket_btree"):
            timed_index(cur, "btree_rebuild",
                        "CREATE INDEX tv_bucket_btree ON tv_items (bucket)",
                        "tv_bucket_btree")
            meta_set(cur, "btree", "built")
        cur.execute("ANALYZE tv_items")
        cur.execute(f"SET hnsw.max_scan_tuples = {MAX_SCAN_TUPLES}")
        cur.execute("SET hnsw.iterative_scan = relaxed_order")
        for sel in SELS:
            plan = pg_explain(cur, SQL, sel, queries[0])
            node = scan_node(plan)
            uses_hnsw = "tv_hnsw_idx" in plan
            results["meta"].setdefault("plan_checks", {})[
                f"pgv_free/sel{sel}"] = {
                "ok": True, "node": node, "uses_hnsw": uses_hnsw,
                "cond_or_filter": [ln.strip() for ln in plan.splitlines()
                                   if "Cond" in ln or "Filter" in ln]}
            dump()
            efs = EFS_PGV if uses_hnsw else [None]
            if uses_hnsw:
                cur.execute(f"SET hnsw.ef_search = {EFS_PGV[-1]}")
            pg_prewarm(cur, SQL, queries, sel)
            for ef in efs:
                if ef is not None:
                    cur.execute(f"SET hnsw.ef_search = {ef}")
                lats, recalls = pg_measure(cur, SQL, queries, truths[sel],
                                           sel)
                rec, med, p90 = summarize(lats, recalls)
                bt, bh, br = pg_buf_samples(cur, SQL, queries, sel)
                add_op(engine="pgv_free", sel=sel, ef=ef, recall=rec,
                       med_ms=med, p90_ms=p90, plan_node=node,
                       uses_hnsw=uses_hnsw, iterative_scan="relaxed_order",
                       buffers_per_query=bt, buffers_hit=bh,
                       buffers_read=br,
                       lats_ms=[round(x, 3) for x in lats])
                print(f"[pgv_free           ] sel={sel:>2}% ef={str(ef):>4} "
                      f"recall={rec:.3f} med={med:6.2f}ms p90={p90:6.2f}ms "
                      f"bufs={bt} [{node[:50]}]", flush=True)


def native_session(cur):
    """Stock-PG baseline hygiene: pg_acorn's planner hook (tier-1 AcornScan
    custom scan over the pgvector index) must NOT intercept the pg-native
    configs. This experiment benchmarks stock pgvector (configs 1-3) vs the
    tier-2 acorn_hnsw AM (configs 4-5), so the hook is off everywhere."""
    cur.execute("SET pg_acorn.enable_hook = off")


def acorn_session(cur, member_first):
    native_session(cur)
    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute("SET enable_sort = off")          # kill btree+sort alt-plan
    cur.execute(f"SET pg_acorn.member_first = {'on' if member_first else 'off'}")
    cur.execute("SET pg_acorn.scan_direct_dist = on")
    cur.execute("SET pg_acorn.scan_single_read = on")
    cur.execute("SET pg_acorn.scan_visited_oneprobe = on")
    cur.execute("SET pg_acorn.scan_direct_filter = on")
    cur.execute("SET pg_acorn.scan_prefetch = off")


def stage_acorn(conn, queries, truths, variant, gamma, postfilter_too):
    """Configs 4/5: acorn in-filter (+ postfilter mode for g2)."""
    with conn.cursor() as cur:
        drop_idx(cur, "tv_hnsw_idx")        # only one vector index at a time
        meta_set(cur, "hnsw", "absent")
        ensure_acorn(cur, variant, gamma)
        cur.execute("ANALYZE tv_items")

    engine = f"acorn_{variant}_infilter"
    with conn.cursor() as cur:
        acorn_session(cur, member_first=True)

        def plan_fn(sel):
            plan = pg_explain(cur, SQL, sel, queries[0])
            return plan_check(
                f"{engine}/sel{sel}", plan,
                must_re=[r"Index Scan using tv_acorn_idx",
                         r"Index Cond: \(bucket <"])

        run_cells(cur, engine, SQL, queries, truths, EFS_ACORN,
                  set_ef=lambda ef: cur.execute(
                      f"SET pg_acorn.ef_search = {ef}"),
                  extra_fields={"member_first": True, "gamma": gamma},
                  plan_fn=plan_fn)

    if not postfilter_too:
        return
    engine = f"acorn_{variant}_postfilter"
    with conn.cursor() as cur:
        acorn_session(cur, member_first=False)

        def plan_fn2(sel):
            plan = pg_explain(cur, SQL_WRAP, sel, queries[0])
            return plan_check(
                f"{engine}/sel{sel}", plan,
                must_re=[r"Index Scan using tv_acorn_idx",
                         r"Filter: \(\(bucket \+ 0\) <"],
                must_not_re=[r"Index Cond"])

        run_cells(cur, engine, SQL_WRAP, queries, truths, EFS_ACORN,
                  set_ef=lambda ef: cur.execute(
                      f"SET pg_acorn.ef_search = {ef}"),
                  extra_fields={"member_first": False, "gamma": gamma,
                                "filter_pushdown": False},
                  plan_fn=plan_fn2)


# ---------------------------------------------------------------- report
PG_NATIVE = ["pg_prefilter", "pgv_post_relaxed", "pgv_post_strict",
             "pgv_free"]
ACORN = ["acorn_g1_infilter", "acorn_g2_infilter", "acorn_g2_postfilter"]
Y1_PAGES_BEFORE, Y1_PAGES_AFTER = 66.0, 2.0


def best_op(engine, sel):
    ops = [o for o in results["ops"]
           if o["engine"] == engine and o["sel"] == sel]
    if not ops:
        return None, None
    ok = [o for o in ops if o["recall"] >= 0.95]
    if ok:
        return min(ok, key=lambda o: o["med_ms"]), max(
            ops, key=lambda o: o["recall"])
    return None, max(ops, key=lambda o: o["recall"])


def report():
    print("\n" + "=" * 78)
    print("THESIS VALIDATION — best op point reaching recall >= 0.95 per "
          "selectivity")
    print("=" * 78)
    for sel in SELS:
        print(f"\n--- selectivity ~{sel}% ---")
        rows = []
        for eng in PG_NATIVE + ACORN:
            b, mx = best_op(eng, sel)
            if b:
                rows.append((eng, b))
                print(f"  {eng:22s} med={b['med_ms']:8.2f}ms "
                      f"p90={b['p90_ms']:8.2f}ms ef={str(b['ef']):>4} "
                      f"recall={b['recall']:.3f}")
            elif mx:
                print(f"  {eng:22s} never reaches 0.95 "
                      f"(max recall={mx['recall']:.3f} "
                      f"@ med={mx['med_ms']:.2f}ms ef={mx['ef']})")
        nat = [(e, b) for e, b in rows if e in PG_NATIVE]
        aco = [(e, b) for e, b in rows if e in ACORN]
        if nat and aco:
            bn = min(nat, key=lambda r: r[1]["med_ms"])
            ba = min(aco, key=lambda r: r[1]["med_ms"])
            ratio = bn[1]["med_ms"] / ba[1]["med_ms"]
            winner = "pg_acorn" if ratio > 1.0 else "pg-native"
            print(f"  >> best native: {bn[0]} {bn[1]['med_ms']:.2f}ms | "
                  f"best acorn: {ba[0]} {ba[1]['med_ms']:.2f}ms | "
                  f"winner: {winner} ({max(ratio, 1/ratio):.2f}x)")
        elif nat and not aco:
            print("  >> acorn never reaches 0.95 here: pg-native wins")

    print("\nY1 projection (hop cost ~66 -> ~2 pages/expansion; latency "
          "scaled by projected/measured pages — optimistic bound that "
          "assumes hot-cache latency tracks buffer touches):")
    for sel in SELS:
        for eng in ["acorn_g1_infilter", "acorn_g2_infilter"]:
            b, _ = best_op(eng, sel)
            if b and b.get("buffers_per_query"):
                proj = b["med_ms"] * (Y1_PAGES_AFTER / Y1_PAGES_BEFORE)
                print(f"  sel={sel:>2}% {eng:18s} med={b['med_ms']:8.2f}ms "
                      f"bufs={b['buffers_per_query']:9.1f} "
                      f"-> Y1-projected ~{proj:7.2f}ms")


# ---------------------------------------------------------------- main
def main():
    global OUT, N, NQ, SELS, EFS_ACORN, EFS_PGV
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="load,prefilter,postfilter,free,"
                    "acorn_g1,acorn_g2")
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        N, NQ = 20_000, 10
        SELS = [1, 5, 20]
        EFS_ACORN = [40, 100]
        EFS_PGV = [40, 100]
    OUT = args.out or ("bench/results_thesis_smoke.json" if args.smoke
                       else "bench/results_thesis_250k.json")
    stages = args.stages.split(",")

    if os.path.exists(OUT) and not args.reload:
        with open(OUT) as f:
            prev = json.load(f)
        results["meta"] = prev.get("meta", {})
        stage_prefixes = {"prefilter": ["pg_prefilter"],
                          "postfilter": ["pgv_post"], "free": ["pgv_free"],
                          "acorn_g1": ["acorn_g1"], "acorn_g2": ["acorn_g2"]}
        drop = [p for s in stages for p in stage_prefixes.get(s, [])]
        results["ops"] = [o for o in prev.get("ops", [])
                          if not any(o["engine"].startswith(p)
                                     for p in drop)]
        print(f"[resume] kept {len(results['ops'])} prior ops from {OUT}",
              flush=True)

    results["meta"].update({
        "n": N, "dim": DIM, "nq": NQ, "k": K, "sels": SELS,
        "efs_acorn": EFS_ACORN, "efs_pgvector": EFS_PGV,
        "metric": "cosine", "correlation": "high (dominant-block)",
        "max_scan_tuples": MAX_SCAN_TUPLES, "stages_this_run": stages,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    dump()

    print(f"[fixture] generating n={N} dim={DIM} ...", flush=True)
    vecs, buckets, queries = make_fixture()
    selcounts = {}
    for sel in SELS:
        cnt = int((buckets < sel).sum())
        selcounts[sel] = {"rows": cnt, "frac_pct": round(100 * cnt / N, 2)}
        print(f"[fixture] bucket<{sel} -> {cnt} rows "
              f"({100*cnt/N:.2f}%)", flush=True)
    results["meta"]["sel_counts"] = selcounts
    print("[fixture] computing exact ground truth ...", flush=True)
    truths = {sel: [exact_truth(vecs, buckets, q, sel) for q in queries]
              for sel in SELS}

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    pg_verify_server(conn)

    if "load" in stages:
        stage_load(conn, vecs, buckets, force=args.reload)
    if "prefilter" in stages:
        stage_prefilter(conn, queries, truths)
    if "postfilter" in stages:
        stage_postfilter(conn, queries, truths)
    if "free" in stages:
        stage_free(conn, queries, truths)
    if "acorn_g1" in stages:
        stage_acorn(conn, queries, truths, "g1", 1, postfilter_too=False)
    if "acorn_g2" in stages:
        stage_acorn(conn, queries, truths, "g2", 2, postfilter_too=True)
    conn.close()

    results["meta"]["finished_utc"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    dump()
    report()
    print(f"\nresults written to {OUT}", flush=True)


if __name__ == "__main__":
    main()
