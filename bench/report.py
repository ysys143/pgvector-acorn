#!/usr/bin/env python3
"""Generate comparison tables and graphs from bench/results.json.

Usage:
    python bench/report.py --input bench/results.json --output bench/report/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt


SELECTIVITIES = [1, 5, 10, 40, 80]
TARGET_ORDER = ["pgvector", "pg_acorn_tier1_g1", "pg_acorn_tier2_g1",
                "pg_acorn_tier2_g2", "qdrant"]
COLORS = {
    "pgvector":          "#e74c3c",
    "pg_acorn_tier1_g1": "#3498db",
    "pg_acorn_tier2_g1": "#2ecc71",
    "pg_acorn_tier2_g2": "#27ae60",
    "qdrant":            "#9b59b6",
}


def scenario_a_table(results: dict) -> str:
    lines = ["## Scenario A: Filter Selectivity Sweep\n"]
    header = f"{'Target':<24} " + " ".join(f"{s:>6}%" for s in SELECTIVITIES)
    lines.append(header)
    lines.append("-" * len(header))
    for target in TARGET_ORDER:
        if target not in results or "a" not in results[target]:
            continue
        a = results[target]["a"]
        row = f"{target:<24} " + " ".join(
            f"{a[str(s)]['recall_mean']:>6.3f}" if str(s) in a
            else f"{a[s]['recall_mean']:>6.3f}" if s in a
            else "     -"
            for s in SELECTIVITIES
        )
        lines.append(row)
    return "\n".join(lines)


def scenario_a_pages_table(results: dict) -> str:
    """Per-query logical page accesses (shared hit + read) — the paper's
    headline cost metric for filtered vector search in a DBMS."""
    lines = ["## Scenario A: Page Accesses per Query (shared hit + read)\n"]
    header = f"{'Target':<24} " + " ".join(f"{s:>7}%" for s in SELECTIVITIES)
    lines.append(header)
    lines.append("-" * len(header))
    any_row = False
    for target in TARGET_ORDER:
        if target not in results or "a" not in results[target]:
            continue
        a = results[target]["a"]
        cells = []
        for s in SELECTIVITIES:
            row = a.get(str(s), a.get(s, {}))
            v = row.get("pages_total_mean")
            cells.append(f"{v:>8.0f}" if isinstance(v, (int, float)) else "       -")
        lines.append(f"{target:<24} " + " ".join(cells))
        any_row = True
    if not any_row:
        lines.append("(no page-I/O data)")
    return "\n".join(lines)


def scenario_b_table(results: dict) -> str:
    lines = ["## Scenario B: Post-Filter Recall (pgvector CTE workaround)\n"]
    if "pgvector" not in results or "b" not in results["pgvector"]:
        return "\n".join(lines + ["(not run)"])
    b = results["pgvector"]["b"]
    lines.append(f"{'Multiplier':<12} {'Recall@10':>10} {'Returned':>10} {'top-k ok':>10}")
    lines.append("-" * 46)
    for mult, row in sorted(b.items(), key=lambda x: int(x[0])):
        lines.append(
            f"{mult+'x':<12} {row['recall_mean']:>10.3f} "
            f"{row['avg_returned']:>10.1f} {str(row['topk_guaranteed']):>10}"
        )
    return "\n".join(lines)


def plot_scenario_a(results: dict, outdir: Path) -> None:
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(18, 5))

    for target in TARGET_ORDER:
        if target not in results or "a" not in results[target]:
            continue
        a = results[target]["a"]
        sels = [s for s in SELECTIVITIES if s in a or str(s) in a]
        recalls = [a.get(s, a.get(str(s), {})).get("recall_mean", None) for s in sels]
        qps = [a.get(s, a.get(str(s), {})).get("qps", None) for s in sels]
        pages = [a.get(s, a.get(str(s), {})).get("pages_total_mean", None) for s in sels]

        color = COLORS.get(target, "gray")
        ax1.plot(sels, recalls, marker="o", label=target, color=color)
        ax2.plot(sels, qps,     marker="o", label=target, color=color)
        # page-I/O may be absent (Qdrant) — only plot points we actually have
        psels = [s for s, p in zip(sels, pages) if p is not None]
        pvals = [p for p in pages if p is not None]
        if pvals:
            ax3.plot(psels, pvals, marker="o", label=target, color=color)

    ax1.set_xlabel("Filter selectivity (%)")
    ax1.set_ylabel("Recall@10")
    ax1.set_title("Recall vs Selectivity")
    ax1.axhline(0.9, ls="--", color="gray", alpha=0.5, label="target=0.9")
    ax1.legend(fontsize=8)
    ax1.set_ylim(0, 1.05)

    ax2.set_xlabel("Filter selectivity (%)")
    ax2.set_ylabel("QPS")
    ax2.set_title("Throughput vs Selectivity")
    ax2.legend(fontsize=8)

    ax3.set_xlabel("Filter selectivity (%)")
    ax3.set_ylabel("Pages per query (shared hit + read)")
    ax3.set_title("Page Accesses vs Selectivity")
    ax3.legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(outdir / "scenario_a.png", dpi=150)
    plt.close(fig)


def plot_scenario_c(results: dict, outdir: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))

    labels: list[str] = []
    for target in TARGET_ORDER:
        if target not in results or "c" not in results[target]:
            continue
        rounds = results[target]["c"]["rounds"]
        labels = [r["label"] for r in rounds]
        recalls = [r["recall_mean"] for r in rounds]
        color = COLORS.get(target, "gray")
        ax.plot(range(len(labels)), recalls, marker="o", label=target, color=color)

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("Recall@10")
    ax.set_title("Recall after Incremental Inserts")
    ax.axhline(0.85, ls="--", color="gray", alpha=0.5, label="threshold=0.85")
    ax.legend(fontsize=8)
    ax.set_ylim(0, 1.05)
    fig.tight_layout()
    fig.savefig(outdir / "scenario_c.png", dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="bench/results.json")
    parser.add_argument("--output", default="bench/report")
    args = parser.parse_args()

    results = json.loads(Path(args.input).read_text())
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    report_lines = ["# pg_acorn Benchmark Report\n"]
    report_lines.append(scenario_a_table(results))
    report_lines.append("\n")
    report_lines.append(scenario_a_pages_table(results))
    report_lines.append("\n")
    report_lines.append(scenario_b_table(results))

    (outdir / "report.md").write_text("\n".join(report_lines))
    print((outdir / "report.md").read_text())

    plot_scenario_a(results, outdir)
    plot_scenario_c(results, outdir)
    print(f"\nCharts saved to {outdir}/")


if __name__ == "__main__":
    main()
