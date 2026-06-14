# auto-ef (pg_acorn.target_recall) — recall validation (250K, correlated)

Date: 2026-06-14. Harness: `bench/auto_ef_sweep.py`. Result:
`bench/results_auto_ef_sweep.json`. Same correlated 250K fixture (seed 0, dim
128, cosine, `bucket < sel` semantics) as the Qdrant/gamma work. Index:
`tv_acorn_autoef` (m=16, ef_construction=64, acorn_gamma=2, payload_edges,
non-inline) built with the auto-ef binary so it carries the filter histogram.
Recall is deterministic (host-independent).

## What auto-ef does

`pg_acorn.target_recall > 0` makes a Tier-2 scan estimate the query's filter
selectivity from a build-time equi-depth histogram of the filter column (stored
in the meta page) and derive ef from a coarse monotone heuristic
(`acorn_am.h`), instead of the manual `pg_acorn.ef_search`. It removes the
per-selectivity ef-tuning burden. It is a convenience, NOT a recall guarantee.

## Result — achieved recall@10

`ef_search` is pinned at a deliberately tiny 40 throughout; with
`target_recall > 0` auto-ef overrides it. The baseline row is `target_recall=0`
(manual ef=40, no auto), showing the collapse auto-ef fixes.

| config            | sel 1% | sel 5% | sel 10% | sel 20% | mean  |
|-------------------|-------:|-------:|--------:|--------:|------:|
| manual ef=40      |  0.802 |  0.345 |   0.253 |   0.183 | 0.396 |
| auto target 0.90  |  0.980 |  0.838 |   0.868 |   0.903 | 0.897 |
| auto target 0.95  |  0.980 |  0.930 |   0.948 |   0.965 | 0.956 |
| auto target 0.99  |  1.000 |  0.995 |   1.000 |   1.000 | 0.999 |

Effective ef chosen by the heuristic (expansions, via `pg_acorn_scan_stats`):

| target | sel 1% | sel 5% | sel 10% | sel 20% |
|--------|-------:|-------:|--------:|--------:|
| 0.90   |    100 |    157 |     314 |     606 |
| 0.95   |    100 |    222 |     444 |     857 |
| 0.99   |    224 |    497 |     994 |    1917 |

## Verdict

- **Mean achieved recall tracks the target almost exactly** (0.897 / 0.956 /
  0.999 for targets 0.90 / 0.95 / 0.99) — with ZERO manual per-selectivity ef
  tuning. A fixed ef=40 collapses from 0.80 at sel 1% to 0.18 at sel 20%; auto-ef
  holds the requested level. That collapse is precisely the drudgery auto-ef
  removes.
- **ef scales correctly** with both selectivity (more passing rows -> larger ef)
  and the recall target, matching the heuristic design.
- **target 0.95 and 0.99 are met or within ~0.02 everywhere**; 0.99 is reliably
  >= 0.995. target 0.90 mildly UNDERSHOOTS at mid-selectivity (0.84 at sel 5%,
  0.87 at sel 10%) on this HARD correlated fixture — the heuristic's sub-anchor
  recall factor (rf < 1 below 0.95) shrinks ef a touch too far for the hardest
  band.

## Honest framing

- The constants are anchored at recall 0.95 (where the heuristic lands dead-on,
  mean 0.956). They are deliberately NOT tuned to erase the target-0.90
  mid-selectivity undershoot: that would overfit a single fixture, which is
  exactly what the coarse-heuristic choice (vs a fixture-calibrated table) was
  made to avoid. On a uniform/independent filter (the easy case) all targets
  track tighter.
- Validated on one correlated fixture (the hard case), 250K, dim 128, one host.
  Auto-ef supports int4 filter columns in v1; other types fall back to manual ef.
