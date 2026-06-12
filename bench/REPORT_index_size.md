# Index Size Study: the cost of inline vector co-location (n=250K)

Date: 2026-06-12. State: post Z3+Z4 merge (`08d993d`). Artifacts:
`bench/noinline_ab.py`, `bench/build_noinline_250k.py`,
`bench/results_noinline_ab.json`.

## Question

The inline configuration (`acorn_inline_vectors=on`) wins every money cell
against pgvector (prefilter, postfilter, iterative scan), but the 250K index
is 4,057 MB — 20.5x stock pgvector and already past `shared_buffers` (2GB).
Is the inline layout load-bearing for that speed, or can the non-inline
layout (TID-only neighbor lists) compete at a fraction of the size?

## Setup

Same table (`tv_items`, n=250K, dim=128, bucket 0..99), same session, same
deterministic fixture as `thesis_validation.py` (FAIR-1T). Three indexes:

| Index | Definition | Size | Build |
|---|---|---|---|
| stock pgvector | hnsw m=16 efc=64 (pgvec_iterative container) | 198 MB | — |
| acorn non-inline | acorn_hnsw m=16 efc=64 gamma=2 payload_edges, inline=off | **249 MB** | 570.3 s (4 workers) |
| acorn inline | same, inline=on | **4,057 MB** | 1,653 s (4 workers) |

Both acorn indexes coexist on the table; each mode is measured inside a
transaction that `DROP INDEX`es the competitor and rolls back, so the planner
choice is forced without rebuilds. Plan shape asserted per cell
(`Index Scan using <index>` + `Index Cond`). passes=3, nq=40, prewarm pass +
NWARM, medians; one `EXPLAIN (ANALYZE, BUFFERS)` sample per cell.

## Where the 4 GB goes (layout model, verified against measurements)

Per layer-0 element at dim=128, m=16, gamma=2 (m_eff=32, 64 layer-0 slots),
from `acorn_t2_page.h`:

| Component | Bytes/element | 250K total |
|---|---|---|
| element tuple (80B hdr + fp32 vector) | 600 | 150 MB |
| neighbor TID array (L0) | 392 | ~100 MB (incl. upper levels) |
| inline entries: 64 x MAXALIGN(40+128) | 10,752 | 2,688 MB |
| page-granularity waste (8,136B primary chunk = 1 page/element; 3,048B continuation tuples pack 2/page) | ~4,800 | ~1.1 GB |

The dominant term is per-edge duplication: each node's SQ8 code (128B) is
stored once per in-edge — on average 64 copies. Unique code data is 32 MB;
the index stores 2 GB of copies (64x). The non-inline index (249 MB,
16.3x smaller, 1.26x stock) confirms the model: everything beyond base
structures is duplication plus the page waste it forces.

## Query A/B (medians, ms; buf = shared page accesses/query, all cache hits)

| sel | ef | inline rec | inline med | inline buf | noinline rec | noinline med | noinline buf |
|----:|----:|---:|---:|---:|---:|---:|---:|
| 1% | 100 | 0.978 | 1.78 | 386 | 0.975 | 9.81 | 5,081 |
| 1% | 200 | 1.000 | 3.52 | 586 | 1.000 | 68.70 | 8,727 |
| 1% | 400 | 1.000 | 5.05 | 986 | 1.000 | 148.61 | 15,100 |
| 1% | 800 | 1.000 | 10.41 | 1,786 | 1.000 | 300.19 | 26,773 |
| 1% | 1600 | 1.000 | 90.80 | 3,386 | 1.000 | 795.71 | 47,899 |
| 10% | 100 | 0.518 | 2.34 | 389 | 0.480 | 30.25 | 6,008 |
| 10% | 200 | 0.748 | 3.01 | 591 | 0.725 | 121.53 | 11,401 |
| 10% | 400 | 0.943 | 23.62 | 991 | 0.938 | 336.20 | 21,009 |
| 10% | 800 | 0.990 | 72.76 | 1,791 | 0.993 | 790.23 | 36,849 |
| 10% | 1600 | 1.000 | 193.46 | 3,389 | 1.000 | 1,535.47 | 60,938 |
| 20% | 100 | 0.367 | 1.51 | 386 | 0.350 | 13.40 | 6,203 |
| 20% | 200 | 0.603 | 4.86 | 586 | 0.575 | 164.99 | 11,809 |
| 20% | 400 | 0.850 | 15.30 | 988 | 0.820 | 463.30 | 22,142 |
| 20% | 800 | 0.960 | 72.23 | 1,788 | 0.963 | 631.03 | 39,929 |
| 20% | 1600 | 1.000 | 248.28 | 3,389 | 0.995 | 183.34 | 67,528 |

Noise note: noinline medians at ef>=800 carry high variance (p90 up to 6.6s;
the sel=20 ef=1600 median of 183 ms is an artifact of that variance — its
p90 is 1,838 ms). Buffer counts are deterministic and monotonic; conclusions
below rest on those plus the consistent ef<=800 cells.

## Decomposition: the slowdown is the PG buffer tax, not ACORN

Both modes execute the same traversal (same graph, same expansions, same
distance count; recalls match within noise). The only difference is page
accesses per query: 36,849 vs 1,791 at sel=10% ef=800 — with `read=0`
(the 249 MB index is fully cached). The 717 ms gap over ~35K extra accesses
prices one buffer-manager access (pin/lock/tuple parse) at **~20 us**.
The same work is a ~0.1 us pointer dereference in an in-memory ACORN.

Cross-check: stock pgvector pays the identical tax — its per-neighbor
element reads put postfilter at 425-623 ms and iterative scan at 467-737 ms
(ef=800, `results_iterative_scan.json`), the same band as non-inline acorn
(300-790 ms). Inline wins not because of ACORN per se but because it bypasses
the per-neighbor access tax; per-edge duplication is the price of that bypass.

## Verdict

- Non-inline is **not competitive**: at sel=10% recall 0.99 it takes 790 ms
  vs prefilter's 225 ms. Size-optimal config loses the thesis claim.
- Inline is load-bearing for every win, and its 16x size (and >shared_buffers
  footprint at 250K, ~16 GB extrapolated at 1M) is the scaling blocker.
- ACORN's intrinsic overhead is only the gamma=2 edge doubling (~2x accesses
  vs stock); the remaining ~10x is PostgreSQL buffer-manager overhead.

Consequence: a sidecar that merely deduplicates codes into regular pages
would still pay ~20 us per neighbor and land at non-inline speed. The fix
must remove the per-neighbor buffer access itself — see
`docs/code-cache-design.md` (per-index shared-memory SQ8 code cache,
DiskANN-style: codes once in RAM, graph on disk).

## Operational notes

- The bench compose service mounts a worktree as `/workspace`; during this
  study the z3 worktree was detached at the merge commit `08d993d` so the
  container runs merged main. Repoint the mount (or re-create the compose
  project from the main checkout) before relying on `docker restart` to pick
  up main.
- Parallel build only engages when `CREATE INDEX` runs as a standalone
  statement with `max_parallel_maintenance_workers` and the table's
  `parallel_workers` reloption set (see `bench/build_noinline_250k.py`);
  the earlier serial runs were caused by a stale (pre-Z4) .so, not by the
  session recipe.
- `tv_acorn_noinline` (249 MB) is kept alongside `tv_acorn_idx` in the bench
  container for future A/Bs.
