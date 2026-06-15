"""Scaling-study report: read results_scale_<N>.json for each N and emit the
build-time / recall / latency-distribution / throughput tables vs N.

Build times are passed via --builds (JSON {N: {pgv_hnsw_s, acorn_g2p64_s,
pgv_hnsw_mb, acorn_g2p64_mb}}) since they come from the run logs.

  uv run --with numpy python3 bench/scale_report.py \
    --dir ~/scale_data --ns 100000,1000000,10000000
"""
import argparse
import json
import os

SELS = [1, 2, 5, 10, 20]
TARGET = 0.94


def pick(cells):
    ok = [c for c in cells if c["recall"] >= TARGET]
    return min(ok, key=lambda c: c["ef"] or 0) if ok else max(cells, key=lambda c: c["recall"])


def peak(by_conc):
    b = max(by_conc.items(), key=lambda kv: kv[1]["qps"])
    return b[1]["qps"], int(b[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/scale_data"))
    ap.add_argument("--ns", default="100000,1000000,10000000")
    ap.add_argument("--builds", default="")
    args = ap.parse_args()
    ns = [int(x) for x in args.ns.split(",")]
    builds = json.loads(args.builds) if args.builds else {}

    data = {}
    for n in ns:
        p = os.path.join(args.dir, f"results_scale_{n}.json")
        if os.path.exists(p):
            data[n] = json.load(open(p))

    def label(n):
        return f"{n//1000}K" if n < 1_000_000 else f"{n//1_000_000}M"

    print("\n## Build time + index size vs N\n")
    print("| N | pgvector hnsw | acorn g2p64 |")
    print("|---|---|---|")
    for n in ns:
        b = builds.get(str(n), {})
        pv = f"{b.get('pgv_hnsw_s','?')}s / {b.get('pgv_hnsw_mb','?')}MB"
        ac = f"{b.get('acorn_g2p64_s','?')}s / {b.get('acorn_g2p64_mb','?')}MB"
        print(f"| {label(n)} | {pv} | {ac} |")

    # recall + latency dist at matched-recall, per engine, per (N, sel)
    engines = [("acorn_g2p64", "pg_latency"), ("pgv_iterative", "pg_latency"),
               ("pgv_prefilter", "pg_latency"), ("qdrant", "qdrant_latency")]
    print("\n## Recall + latency (matched-recall ~0.95) — recall | median | p95 | p99 ms\n")
    for sel in [1, 10, 20]:
        print(f"\n### sel = {sel}%\n")
        print("| N | " + " | ".join(e for e, _ in engines) + " |")
        print("|---|" + "---|" * len(engines))
        for n in ns:
            if n not in data:
                continue
            row = [label(n)]
            for eng, sect in engines:
                cells = (data[n].get(sect, {}).get(eng) if eng != "qdrant"
                         else data[n].get(sect, {})).get(str(sel)) if eng != "qdrant" \
                    else data[n].get("qdrant_latency", {}).get(str(sel))
                if not cells:
                    row.append("-"); continue
                c = pick(cells)
                row.append(f"{c['recall']:.2f} \\| {c['med_ms']} \\| {c['p95_ms']} \\| {c['p99_ms']}")
            print("| " + " | ".join(row) + " |")

    print("\n## Peak throughput (QPS) vs N\n")
    print("| N | engine/sel | peak QPS @conc |")
    print("|---|---|---|")
    for n in ns:
        if n not in data:
            continue
        tp = data[n].get("throughput", {})
        for cfg, d in tp.items():
            q, cc = peak(d["by_conc"])
            print(f"| {label(n)} | {cfg} sel{d['sel']}% | {q:.0f} @{cc} |")


if __name__ == "__main__":
    main()
