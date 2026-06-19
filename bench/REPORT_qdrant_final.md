# acorn vs Qdrant — corrected, complete verdict (250K, correlated fixture)

> **2026-06-19:** for the reconciled competitive position (this report's
> "1.6-4.4x" is cross-substrate/INDICATIVE; `OVERHEAD_LEDGER.md` refines it),
> cite `bench/COMPETITIVE_VERDICT.md` as the single source of truth.

Date: 2026-06-14. Supersedes the recall claim in `REPORT_qdrant_rematch.md`
(which used pre-Z3 acorn numbers). Sources: `results_qdrant_rematch.json`
(Qdrant HNSW forced, gate recall 0.302), `results_gamma_sweep.json` (acorn
recall, deterministic), `results_gamma_latency.json` (acorn latency, current
binary, code cache ON + prefetch, quiet host). Same correlated 250K fixture,
queries, exact truth, `bucket < sel` semantics, m=16 ef_construct=64.

## How the earlier verdict was wrong

`REPORT_qdrant_rematch.md` concluded "Qdrant beats acorn — acorn caps
~0.91-0.94 at sel 10/20%". That used `results_thesis_250k.json`, whose recall
caps were the emission-order bug Z3 fixed, not a graph limit. With the current
binary acorn reaches 0.99/0.96 (g2 ef800) and ~1.0 (ef1600) — confirmed by the
gamma sweep and cross-checked against post-Z3 `results_emission_250k_quiet`.

## Matched-recall comparison (min_ms — host-robust floor; recall >= ~0.95)

| sel | acorn best (gamma, ef, recall) | acorn min_ms | Qdrant (ef, recall) | Qdrant min_ms | ratio |
|----:|---|---:|---|---:|---:|
| 1% | g4 ef100 r1.00 | 3.6 | ef100 r0.98 | 3.9 | **0.9x (acorn)** |
| 2% | g4 ef100 r0.99 | 4.0 | ef100 r0.99 | 4.0 | 1.0x (tie) |
| 5% | g4 ef200 r0.99 | 10.2 | ef200 r0.98 | 6.2 | 1.6x (Qdrant) |
| 10% | g3 ef400 r0.98 | 30 | ef400 r0.98 | 8.1 | 3.7x (Qdrant) |
| 20% | g4 ef400 r0.96 | 57 | ef800 r0.97 | 13.1 | 4.4x (Qdrant) |

## Verdict

- **Recall: acorn = Qdrant.** Both reach 0.97-1.0 at the money cells on the
  hard correlated fixture. The earlier "recall gap" was a stale-data artifact.
- **Latency: tie at low selectivity (1-2%), Qdrant faster at high (5-20%),**
  the gap growing 1.6x -> 4.4x with selectivity. At sel 1-2% acorn (in
  PostgreSQL, code-cache served) is at parity with a dedicated in-memory
  engine on both axes. At higher selectivity — more passing rows, more search
  work — Qdrant's in-memory Rust path (no buffer-manager/MVCC per expansion)
  pulls ahead.
- **gamma matters:** gamma=4 reaches a recall target at lower ef than gamma=2,
  and is modestly faster at high selectivity at matched recall (sel=20% r~0.95:
  g4 ef400 57 ms vs g2 ef800 82 ms). It narrows but does not close the gap.
  gamma 3/4 were never benchmarked before this; gamma=2 (the prior default
  test point) under-sold acorn. **Note:** gamma=4's extra density is half-wasted
  on the global half (`qdrant-borrow-list.md`); the independent `acorn_payload_m`
  reloption (borrow P1) is the more efficient lever — same recall at ~1.7-2.8x
  lower latency (`REPORT_payload_m.md`).

## Honest framing

- The remaining gap is the expected in-Postgres-vs-dedicated-engine cost, and
  it appears only at high selectivity. acorn matching Qdrant on recall
  everywhere and on latency at low selectivity — inside PostgreSQL, with
  transactions/SQL/joins and a 289 MB footprint — is a strong result, not a
  loss. acorn's thesis ("best filtered ANN inside Postgres; beats pgvector")
  stands; "competitive with a dedicated engine" is now substantially true at
  low selectivity and within a small multiple at high selectivity.
- Caveats: Qdrant ran from the host (in-memory); acorn in PostgreSQL — still
  different substrates, but now both are compact-index + fast-path so the
  comparison is far fairer than the earlier inline-vs-Qdrant. Single
  correlated fixture (the hard case); 250K; one dev host; min-of-reps.
- Closing the high-selectivity latency gap is an acorn engineering target
  (per-expansion overhead reduction; roadmap Phase 2 filter-aware traversal),
  not a recall problem.
