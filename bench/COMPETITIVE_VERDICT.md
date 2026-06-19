# acorn vs Qdrant / pgvector — reconciled competitive verdict (single source of truth)

> Status: **2026-06-19 reconciliation.** This document supersedes the scattered
> and partly conflicting competitive claims in `REPORT_qdrant_final.md`,
> `REPORT_qdrant_rematch.md`, `REPORT_3way.md`, `REPORT_scale.md`, and
> `OVERHEAD_LEDGER.md`. Cite THIS file for the competitive position; the others
> are the underlying measurements.

## TL;DR

- **vs pgvector (in Postgres): acorn wins, unambiguously.** Best filtered-ANN
  inside PostgreSQL — beats prefilter/postfilter/iterative on recall at every
  selectivity (e.g. 1M sel 1%: acorn r0.53 vs pgvector r0.043, ~12x;
  `REPORT_n1m.md`, `REPORT_3way.md`). This is acorn's actual thesis.
- **vs Qdrant (dedicated in-memory engine): recall PARITY, latency UNRESOLVED.**
  - **Recall: tie.** Post-Z3, acorn reaches 0.97-1.0 at sufficient ef on the
    hard correlated fixture, matching Qdrant (`REPORT_qdrant_final.md`,
    `results_gamma_sweep.json`). The old "acorn caps ~0.91-0.94" was a stale
    pre-Z3 emission-order bug, NOT a graph limit.
  - **Latency: a real but uncertain gap at high selectivity + matched recall;
    magnitude not settled.** Do NOT cite a single multiplier as fact.

## Why latency is "unresolved" (not a clean number)

Two measurements exist; both are partial and they view different operating points.

1. **`REPORT_qdrant_final.md`** (250K correlated, min_ms, matched recall >=0.95):
   acorn ties Qdrant at sel 1-2% (0.9-1.0x) and trails at high selectivity —
   ratio **1.6x (sel 5%) -> 3.7x (10%) -> 4.4x (20%)**. *Caveat in that report
   itself:* Qdrant runs on the host (in-memory, no PG layer), acorn runs inside
   PostgreSQL (buffer manager + MVCC) — **different substrates, INDICATIVE, single
   correlated fixture, one dev host.** This is the source of the "1.6-4.4x"
   figure (often mis-cited elsewhere as coming from `REPORT_3way.md`).

2. **`OVERHEAD_LEDGER.md`** (30K, hot cache, single client, controlled median-basis):
   decomposes the gap to unit costs and finds:
   - Much of an earlier "4x" headline was **measurement methodology**, not the
     engine: `QPS = N/sum(latencies)` is governed by a fat environmental tail;
     using medians + prepared statements + pre-formatted vector literals
     recovers ~3-5x of reported QPS (low-sel median gap shrinks to **~1.2-1.5x**).
   - At a *fixed low ef* (th=40, gamma=4, ef=10) acorn is **faster** than Qdrant
     (1.29 ms vs 2.0-2.3 ms) but at recall 0.953 vs Qdrant's 1.0 — i.e. it's a
     **recall-QPS frontier tradeoff**, not "acorn is just slower."
   - The honest per-unit engine gap is **~3.8x, and ~90% of it is the
     buffer-manager double-load per neighbor** in `acorn_stream_expand`
     (`acorn_distance()` then `acorn_load_element()` re-read the SAME element
     page). This is a **fixable C change** (ledger route #2, projected ~1.5x:
     ~775 -> ~1170 QPS), not an intrinsic substrate cost.

**Reconciliation:** it is one recall-QPS frontier. At *matched high recall* Qdrant
leads at high selectivity (the 1.6-4.4x, INDICATIVE/cross-substrate/250K). At a
*fixed low ef* acorn can lead on speed but trails on recall. The largest
engine-side latency component is fixable (neighbor double-load); the rest is the
expected in-Postgres-vs-dedicated-engine substrate cost. **The exact magnitude is
not settled** — a clean same-protocol, median-basis (prepared, literal params,
median + p99 reported separately) re-measurement at >=200K is required before any
single multiplier should be quoted. Until then: latency vs Qdrant is INDICATIVE.

## What is solid vs indicative

| Claim | Status |
|---|---|
| acorn beats pgvector (recall, in Postgres, all selectivities) | **SOLID** |
| acorn = Qdrant on recall (0.97-1.0, post-Z3, sufficient ef) | **SOLID** |
| acorn ties Qdrant on latency at low selectivity (sel 1-2%) | likely (two sources agree directionally) |
| Qdrant faster at high selectivity + matched recall | likely, **magnitude INDICATIVE** (1.6-4.4x is cross-substrate, host-jittered, 250K) |
| The largest fixable acorn latency line = neighbor double-load | **SOLID** (ledger unit-cost closure, route #2) |
| A single "Nx slower" number | **DO NOT QUOTE as settled** |

## Open follow-up (out of scope here)
Clean median-basis, same-protocol Qdrant re-measurement at >=200K (prepared
statements, pre-formatted vectors, median + p99 separately, current optimized
binary, both engines on the same substrate where possible). Plus ledger route #2
(neighbor double-load dedup) which is projected to remove ~45% of the acorn scan
exec time. See `OVERHEAD_LEDGER.md` section 3 for the ranked route list.
