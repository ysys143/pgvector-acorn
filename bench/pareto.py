#!/usr/bin/env python3
"""Render recall-QPS Pareto tables from an ef_search-sweep results file.

Consumes the scenario-A structure produced by the ef_search sweep:

    results[target]["a"][selectivity][ef_search] = {recall_mean, qps, p99_ms, ...}

For each selectivity it prints, per target, the (ef -> recall, qps) sweep and
marks the Pareto-optimal points (no other point of the same target has both
higher recall and higher qps). This is the apples-to-apples view: each engine
traces its own frontier by sweeping its own knob, and we compare frontiers, not
ef values.

Usage:
    python bench/pareto.py bench/results_100k_low.json
    python bench/pareto.py bench/results_100k_low.json --selectivity 1
"""

from __future__ import annotations

import argparse
import json


def _as_points(ef_map: dict) -> list[tuple]:
    """[(ef, recall, qps, p99), ...] sorted by ef. JSON keys are strings."""
    pts = []
    for ef_key, row in ef_map.items():
        ef = None if ef_key in ("null", None) else int(ef_key)
        pts.append((ef, row.get("recall_mean"), row.get("qps"), row.get("p99_ms")))
    pts.sort(key=lambda p: (p[0] is None, p[0]))
    return pts


def _pareto_flags(points: list[tuple]) -> list[bool]:
    """A point is Pareto-optimal if no other point has >= recall AND >= qps
    with at least one strictly greater."""
    flags = []
    for i, (_, r_i, q_i, _) in enumerate(points):
        if r_i is None or q_i is None:
            flags.append(False)
            continue
        dominated = False
        for j, (_, r_j, q_j, _) in enumerate(points):
            if i == j or r_j is None or q_j is None:
                continue
            if r_j >= r_i and q_j >= q_i and (r_j > r_i or q_j > q_i):
                dominated = True
                break
        flags.append(not dominated)
    return flags


def render(results: dict, selectivities: list[int]) -> str:
    out: list[str] = []
    targets = list(results.keys())

    for sel in selectivities:
        out.append(f"\n=== selectivity {sel}%  (recall-QPS sweep over ef_search) ===")
        out.append(f"{'target':<22} {'ef':>5} {'recall':>8} {'qps':>9} {'p99_ms':>8}  pareto")
        out.append("-" * 64)
        for target in targets:
            a = results[target].get("a", {})
            ef_map = a.get(str(sel), a.get(sel))
            if not ef_map:
                continue
            points = _as_points(ef_map)
            flags = _pareto_flags(points)
            for (ef, recall, qps, p99), is_par in zip(points, flags):
                ef_s = "-" if ef is None else str(ef)
                r_s = f"{recall:.3f}" if recall is not None else "  -  "
                q_s = f"{qps:.1f}" if qps is not None else "  -  "
                p_s = f"{p99:.2f}" if p99 is not None else "  -  "
                mark = " *" if is_par else ""
                out.append(f"{target:<22} {ef_s:>5} {r_s:>8} {q_s:>9} {p_s:>8} {mark}")
            out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="path to results JSON (ef-sweep format)")
    ap.add_argument("--selectivity", type=int, default=None,
                    help="render a single selectivity (default: all found)")
    args = ap.parse_args()

    results = json.loads(open(args.results).read())

    # discover selectivities present in the first target's scenario-A block
    first = next(iter(results.values()), {})
    sels_present = sorted(int(s) for s in first.get("a", {}).keys())
    selectivities = [args.selectivity] if args.selectivity else sels_present

    print(f"# {args.results}")
    print(f"# targets: {', '.join(results.keys())}")
    print(render(results, selectivities))


if __name__ == "__main__":
    main()
