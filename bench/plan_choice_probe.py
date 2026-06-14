"""P2 safety net: does the planner's plan choice match the latency-optimal one?

acorn_cost.c picks between the acorn KNN index scan and a bitmap PREFILTER
(Bitmap Heap Scan on the filter btree + Sort by distance, exact / recall 1.0)
purely on cost.  This probe measures, across a selectivity grid, BOTH the
planner's cost-based choice AND the real EXPLAIN ANALYZE time of each forced
plan, so we can see where the cost crossover sits vs the actual-time crossover
-- i.e. whether the planner ever prefers acorn where the exact prefilter is
genuinely faster (the low-cardinality case P2 targets), or vice versa.

This is the BASELINE captured BEFORE any cost-model change; re-run after to
confirm the mid-selectivity band (where acorn wins) does not regress.

Run inside the bench postgres container:
  python3 -u /workspace/bench/plan_choice_probe.py
"""
import json
import os
import statistics

import psycopg

from thesis_validation import K, make_fixture, qstr, SQL

DSN = os.environ.get("PG_DSN",
                     "host=/var/run/postgresql dbname=bench user=postgres")
OUT = os.path.join(os.path.dirname(__file__), "results_plan_choice_probe.json")
SELS = [1, 2, 5, 10, 20, 50]
NQ = 10
EF = 100              # fixed manual ef for the acorn path
ACORN_IDX = "tv_acorn_noinline"   # the shipping non-inline index


def classify(plan):
    """Walk a plan tree, return a short label for the access strategy used."""
    found = {"acorn": False, "bitmap": False, "seq": False, "sort": False}

    def walk(node):
        nt = node.get("Node Type", "")
        if nt == "Index Scan" and str(node.get("Index Name", "")).startswith("tv_acorn"):
            found["acorn"] = True
        elif "Bitmap" in nt:
            found["bitmap"] = True
        elif nt == "Seq Scan":
            found["seq"] = True
        elif nt == "Sort":
            found["sort"] = True
        for ch in node.get("Plans", []):
            walk(ch)

    walk(plan["Plan"])
    if found["acorn"]:
        return "acorn"
    if found["bitmap"]:
        return "prefilter" + ("+sort" if found["sort"] else "")
    if found["seq"]:
        return "seqscan" + ("+sort" if found["sort"] else "")
    return "other"


def explain(cur, sel, q):
    cur.execute("EXPLAIN (ANALYZE, FORMAT JSON, TIMING ON, BUFFERS OFF) " + SQL,
                (int(sel), qstr(q), K))
    plan = cur.fetchone()[0][0]
    return classify(plan), plan["Plan"]["Total Cost"], plan["Plan"]["Actual Total Time"]


def run_mode(cur, sel, queries, gucs):
    cur.execute("SET enable_seqscan = on")
    cur.execute("SET enable_bitmapscan = on")
    cur.execute("SET enable_indexscan = on")
    for g, v in gucs.items():
        cur.execute(f"SET {g} = {v}")
    # prewarm
    cur.execute(SQL, (int(sel), qstr(queries[0]), K))
    cur.fetchall()
    labels, costs, times = [], [], []
    for q in queries:
        lab, cost, t = explain(cur, sel, q)
        labels.append(lab); costs.append(cost); times.append(t)
    return labels[0], round(statistics.median(costs), 1), round(statistics.median(times), 2)


def main():
    print("[fixture] correlated seed0 ...", flush=True)
    _, _, queries = make_fixture()
    queries = queries[:NQ]

    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=0)
    cur = conn.cursor()
    cur.execute("SET pg_acorn.member_first = on")
    cur.execute("SET pg_acorn.scan_code_cache = on")
    cur.execute("SET pg_acorn.scan_inline_vectors = on")
    cur.execute(f"SET pg_acorn.ef_search = {EF}")

    # keep only the non-inline acorn index visible so the acorn path is unambiguous
    cur.execute("BEGIN")
    for idx in ["tv_acorn_idx", "tv_acorn_autoef", "tv_acorn_g3", "tv_acorn_g4"]:
        cur.execute(f"DROP INDEX IF EXISTS {idx}")

    out = {"meta": {"fixture": "correlated seed0 250K", "k": K, "ef": EF,
                    "acorn_index": ACORN_IDX, "nq": NQ}, "rows": []}
    print(f"{'sel%':>4} | {'planner':>14} {'cost':>10} {'ms':>8} | "
          f"{'acorn cost':>10} {'ms':>8} | {'prefilter cost':>13} {'ms':>8} | winner",
          flush=True)
    for sel in SELS:
        dl, dc, dt = run_mode(cur, sel, queries, {})
        al, ac, at = run_mode(cur, sel, queries,
                              {"enable_bitmapscan": "off", "enable_seqscan": "off"})
        pl, pc, pt = run_mode(cur, sel, queries,
                              {"enable_indexscan": "off", "enable_seqscan": "off"})
        time_winner = "acorn" if at < pt else "prefilter"
        cost_winner = "acorn" if ac < pc else "prefilter"
        row = {"sel": sel, "planner_pick": dl, "planner_cost": dc, "planner_ms": dt,
               "acorn_cost": ac, "acorn_ms": at, "prefilter_cost": pc, "prefilter_ms": pt,
               "time_winner": time_winner, "cost_winner": cost_winner,
               "mismatch": time_winner != cost_winner}
        out["rows"].append(row)
        flag = "  <-- MISMATCH" if row["mismatch"] else ""
        print(f"{sel:>4} | {dl:>14} {dc:>10} {dt:>8} | {ac:>10} {at:>8} | "
              f"{pc:>13} {pt:>8} | time:{time_winner} cost:{cost_winner}{flag}",
              flush=True)
    cur.execute("ROLLBACK")

    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    conn.close()
    print(f"\n[done] -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
