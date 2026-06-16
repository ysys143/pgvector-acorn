# M-ACORN build-time penalty: findings (negative result)

Branch `feat/macorn-penalty` (prototype commit `d784a1d`, base `e0f266c`).
Investigation date: 2026-06-16/17. Fixture: real Cohere 1024-d embeddings
(`bench.tv_items`), VM `pg-acorn-bench`.

## TL;DR

**M-ACORN's build-time penalty is structurally redundant in pg_acorn.** Under
controlled, deterministic measurement it produces a filtered-recall curve
**identical to plain ACORN** (no gain, no cost) on both correlated and
independent filters. The reason: pg_acorn already solves predicate-subgraph
fragmentation at *scan* time via ACORN's gamma expansion, so a build-time
same-filter edge bias adds nothing. M-ACORN is a fix for *vanilla* HNSW;
pg_acorn is not vanilla HNSW.

The prototype is harmless (GUC default-off, build cost negligible, docker-test
green) but does not earn a place as a default. The branch is kept dormant for
reference; it is not merged to main.

## What M-ACORN is

At neighbor selection, inflate the distance of a candidate whose payload
partition differs from the base node's by `*(1 + factor)`, then re-sort, so
robust-prune prefers same-filter neighbors. Intent: strengthen the predicate
subgraph's connectivity at build time so filtered queries keep more matching
neighbors reachable.

Implementation (see commit `d784a1d`):
- `acorn_mem_macorn_rerank()` in `src/acorn_build.c`, called only on the
  base-case selection path (`acorn_payload_edges` OFF), just before
  `acorn_mem_select_diverse`. Re-sorts in place to preserve the
  ascending-distance precondition.
- GUC `pg_acorn.build_macorn_penalty` (bool, default off) +
  `pg_acorn.build_macorn_penalty_factor` (real, default 1.0).
- Mutually exclusive with `acorn_payload_edges` (ERROR if both on).
- Regress test `tier2_build_macorn`.

## Measurement, and two traps it exposed

All A/B runs build plain (penalty off) and macorn (penalty on) on the SAME 1M
or 200K subset of real Cohere data, then measure filtered recall@10 vs an exact
seqscan ground truth, plus mean index-scan latency, across `ef_search`.

Two measurement traps invalidated early runs; both are now documented so future
filtered-recall A/Bs avoid them:

1. **Ground-truth leak.** The first script computed "exact" truth with
   `enable_indexscan=off` (a *soft* cost penalty) while `enable_seqscan` was
   also off, so the planner still used the same ACORN index to compute the
   "truth" -> recall measured the index against itself (~1.0) and hid all
   signal. Fix: compute truth via seqscan *before any index exists*.

2. **Build non-determinism.** Parallel builds with no `build_seed` produce a
   different graph each run; plain vs macorn then differ by ~0.06 of pure
   build noise, which masqueraded as a +0.045 recall "gain." Fix:
   `max_parallel_maintenance_workers = 0` + `pg_acorn.build_seed = 42` so the
   two graphs differ ONLY by the penalty.

Canonical scripts: `bench/macorn_ab4.sql` (correlated filter),
`bench/macorn_ab5.sql` (independent filter). Both use seqscan-before-index
truth, deterministic serial builds, a warm-up query pass, and an ef sweep
10..400.

## Controlled results (200K, serial+seed=42, warmed, recall@10)

Plain vs macorn differ ONLY by the penalty (deterministic builds).

### Correlated filter (`tv_items.bucket`, derived from the embedding)

| sel | ef | plain | macorn | b3 |
|-----|----|-------|--------|----|
| lt5 (5%) | 10  | 0.206 | 0.208 | 0.159 |
| lt5 | 100 | 0.658 | 0.653 | 0.601 |
| lt5 | 400 | 0.899 | 0.898 | 0.889 |
| eq1 (1%) | 100 | 0.462 | 0.458 | 0.436 |
| eq1 | 400 | 0.751 | 0.753 | 0.749 |

plain == macorn (diff <= 0.007, no direction). B3 <= plain everywhere.

### Independent filter (`abs(hashint4(id)) % 100`, uniform, embedding-independent)

| sel | ef | plain | macorn | b3 |
|-----|----|-------|--------|----|
| lt5 | 10  | 0.266 | 0.264 | 0.206 |
| lt5 | 100 | 0.806 | 0.808 | 0.803 |
| lt5 | 200 | 0.891 | 0.892 | **0.907** |
| lt5 | 400 | 0.940 | 0.939 | **0.957** |
| eq1 | 300 | 0.798 | 0.798 | **0.815** |
| eq1 | 400 | 0.850 | 0.851 | **0.872** |

plain == macorn again, even here -- and the independent filter is exactly the
fragmented regime where M-ACORN *should* help. Latency: plain ~= macorn
throughout; B3 is 1.3-2x slower (more edges per node to traverse).

## Why M-ACORN does nothing here

pg_acorn IS ACORN: at scan time it expands the candidate frontier (gamma) so it
reaches filtered matches without needing the predicate subgraph to be connected
in the base graph. M-ACORN strengthens that same connectivity at build time --
work the ACORN scan already does -- so the two are redundant and the build-time
bias produces no net change. M-ACORN's published gains are over *vanilla* HNSW,
which has no such scan-time expansion.

## Side finding: where B3 (payload_edges) actually helps

The same controlled runs reframe `acorn_payload_edges` (B3):
- **Correlated filter:** B3 <= plain at every ef (same-bucket members already
  cluster, so plain's nearest-neighbor edges connect them; payload edges are
  redundant and only add traversal cost).
- **Independent filter, high ef (>= ~150):** B3 reaches a higher recall ceiling
  than plain (e.g. lt5 ef=400: 0.957 vs 0.940), at ~2x latency.

So B3's value is confined to **independent filters at high-recall operating
points** -- consistent with production indexes running high ef. This is a
200K / recall@10 reading; large-scale (per-bucket subgraphs ~100K at 10M)
re-confirmation is left open.

## Disposition

- Branch kept dormant; not merged. Prototype is default-off and inert.
- Recommendation: do not pursue build-time predicate-edge biasing for pg_acorn
  (redundant with ACORN). Treat filtered recall at scan time (gamma / ef).
- Open follow-up: re-confirm B3's high-ef independent-filter advantage at 10M.

See also `docs/literature-review-2025-06.md` (M-ACORN source) and
`docs/build-perf-backlog.md`.
