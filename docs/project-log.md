# pg_acorn — project log (authoritative experiment ledger)

> One-glance map of all work. Maintained as the entry point for "what has been
> done / tried / concluded". Last consolidated: 2026-06-19 (full audit, 231 commits).
> Detail lives in the linked docs/reports; this is the index.

## Experiment clusters

| # | cluster | dates | what | conclusion | location | on main |
|---|---------|-------|------|------------|----------|:------:|
| 1 | tier2-infilter (core AM) | 06-09 | page format + in-filter ACORN traversal + 2-pass flush + cost model | shipped | main | O |
| 2 | scan-emission (Z3) | 06-11 | buffered emission (finish expansion before emit, ORDER BY contract) | **recall ceiling eliminated** (0.97-1.0) | main | O |
| 3 | colocation (inline SQ8) | 06-11 | inline SQ8 vectors+meta in layer-0 neighbor lists | shipped; 20.5x size cost (`REPORT_index_size`) | main | O |
| 4 | code-cache (SQ8 shmem) | 06-12/13 | per-index shared-mem SQ8 cache M1-M3, prefetch, default ON | shipped; 1.46-1.72x inline latency at ~14x smaller (`code-cache-design`, `REPORT_acceptance_codecache`) | main | O |
| 5 | code-cache concurrency | 06-12/13 | BUG-3 dsa_area double-unpin, eviction churn, hazard ptrs; ef=1600 crash = environmental | fixed | main | O |
| 6 | deterministic build (V1/diversify) | 06-11 | direct distance kernel + `build_seed` + `acorn_diversify` | shipped | main | O |
| 7 | parallel-build acceptance | 06-11/12 | mwm gate + RSS leak fix + worker scaling | **3.01x** speedup, 250K 1653s, gate PASS (cdf0847) | main | O |
| 8 | payload-edges | 06-10/11 | reloption + member-first expansion + validation | shipped | main | O |
| 9 | index-size study | 06-12/13 | inline vs non-inline (4GB vs 249MB) | documented (`REPORT_index_size`) | main | O |
| 10 | Qdrant rematch / gamma-sweep | 06-14 | HNSW-forced Qdrant vs acorn; gamma sweep | **recall PARITY** (acorn does NOT cap, post-Z3); "caps 0.91-0.94" was stale bug (`REPORT_qdrant_rematch` CORRECTION, `REPORT_qdrant_final`) | main | O |
| 11 | Qdrant-borrow features | 06-14/15 | P1 payload_m, P3 auto-ef, P4 payload-gate, P5 telemetry, P2 plan-choice | P1+P3 shipped; P2 no-change; (`qdrant-borrow-list`, `REPORT_payload_m`/`_auto_ef`/`_plan_choice`) | main | O |
| 12 | 3-way + scaling | 06-15 | pgvector vs Qdrant vs acorn, 100K/1M/10M Cohere | recall solid; **latency INDICATIVE** (`REPORT_3way`, `REPORT_scale`) | main | O |
| 13 | overhead-ledger | 06-10 | microsecond decomposition of the Qdrant gap | **measurement methodology inflated the gap**; engine gap ~3.8x, ~90% = neighbor double-load (fixable) (`OVERHEAD_LEDGER`) | main (forward-ported 06-19) | O |
| 14 | 2-hop NaviX-Directed | 06-09/10 | runtime ACORN-1 2-hop + node cache | **shelved** — 200K recall regression, gamma won; node-cache kept, dead GUC removed (`development-roadmap` Track A) | main | O |
| 15 | build-perf (B1-B4/N1-N4) | 06-16 | atomic allocator (B1/B2), two-pass (B3), caps (N2/N3), CV broadcast (N4) | **two-pass not a build lever**; N4 real bug fixed; B1/B2 regression; N1 reverted (`build-perf-backlog`) | feat/build-perf-atomic | **X** |
| 16 | M-ACORN penalty | 06-16/17 | build-time predicate-aware neighbor bias | **negative** — redundant with scan-time gamma (`macorn-penalty-findings`) | feat/macorn-penalty | **X** |
| 17 | extension-lock livelock | 06-17 | (found during M-ACORN 10M) parallel build + mwm-spill livelocks on relation-extension LWLock | documented; fix = serial / bulk pre-extend | main (memory) | O |
| 18 | Release / roadmap / consolidation | 06-17/19 | Phase 0 closed, 5-track roadmap, README/ABOUT, v0.1.0, this audit | v0.1.0 cut | main | O |

## Branch disposition

| branch | state | action |
|--------|-------|--------|
| main | release line (v0.1.0) | active |
| feat/build-perf-atomic | dormant, documented | salvage via GUC-gate (split B1/B2 regression from B3/N2-N4) → **0.1.1**; keep |
| feat/macorn-penalty | dormant, documented (negative) | keep as record |
| feat/overhead-ledger | findings forward-ported to main (06-19) | **archive/delete-able** |
| bench-page-io, feat/scan-emission, feat/tier2-ef-search, tier2-acorn-hnsw-am, tier2-infilter, tier2-production-build | 0 commits ahead of main (merged) | **delete-able** (pending user) |

## Disproven hypotheses (do not re-attempt without new evidence)
- **two-pass payload build** is not a build-time lever (≈ OLD interleaved). [build-perf]
- **M-ACORN build-time penalty** is inert in pg_acorn (scan-time gamma already solves it). [M-ACORN]
- **runtime 2-hop / NaviX-Directed** regresses recall at scale; gamma wins. [2-hop]
- **N1 per-worker batch reservation** worsened the B1 regression (reverted).

## Open / unresolved
- **Qdrant latency gap magnitude** — INDICATIVE; needs clean median-basis, same-protocol re-measurement at >=200K. See `bench/COMPETITIVE_VERDICT.md`. Largest fixable line = neighbor double-load (ledger route #2, ~1.5x).
- **B1/B2 atomic allocator regression** — GUC-gate merge for 0.1.1 (separability analysis done).
- **Build scalability** — extension-lock livelock on mwm-spill; bulk pre-extend + flush parallelization (roadmap Track B).
- Roadmap Track A (query algorithm) is **demoted** (2-hop tried+shelved); Tracks C/B/D are the live frontier.

## Where to look
- Competitive position → `bench/COMPETITIVE_VERDICT.md` (single source of truth)
- Future work → `docs/development-roadmap.md` (5 tracks)
- Build perf → `docs/build-perf-backlog.md`; negatives → `docs/macorn-penalty-findings.md`
- Cross-session memory → project memory `project-map`, `*-finding`, `twohop-shelved`, `build-extension-lock-livelock`
