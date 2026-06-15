# 3-way filtered-KNN benchmark — pgvector native vs Qdrant vs pg_acorn

Date: 2026-06-15. Harnesses: `bench/bench3way_pg.py` (psycopg from host),
`bench/bench3way_qdrant.py` (httpx from host), `bench/bench3way_report.py`
(extraction). Results: `bench/results_3way_pg.json`, `bench/results_3way_qdrant.json`.

Same correlated 250K fixture for all three engines (thesis_validation seed 0,
dim 128, cosine, bucket 0-99, filter `bucket < sel`, top-k=10, exact truth).
pgvector + pg_acorn share the IDENTICAL `tv_items` table in one Postgres
container; Qdrant holds byte-identical data (point id = i+1). m=16,
ef_construct=64 everywhere. Clients run from the host over TCP (psycopg / httpx),
multiprocessing for throughput.

---

## READ THIS FIRST — what is trustworthy here, and what is NOT

This ran on a shared 8-core macOS Docker VM with a co-tenant. That has a hard
consequence for how to read the numbers:

| metric | trust | why |
|---|---|---|
| **recall@10** | SOLID | deterministic; independent of host load. The headline. |
| **relative / qualitative verdict** | SOLID | all engines measured under the SAME noise, so the ordering and the pgvector-iterative failure are real. |
| **absolute latency (ms)** | INDICATIVE ONLY | host jitter inflates median/p95 2-7x; see the min caveat below. NOT a trustworthy absolute. |
| **throughput (QPS)** | INDICATIVE ONLY | noisy concurrency scaling + Python clients + Qdrant HTTP path. Relative shape only. |
| **index size / build time** | SOLID (size) / approx (build) | size is deterministic; build times were partly contended. |

### Why `min_ms` is NOT a legitimate headline latency (self-correction)

An earlier draft of this report led with `min_ms` as "the host-robust floor." That
was overreach. `min` is a defensible *floor estimate* but a poor *comparison basis*:

- It is an **extreme order statistic** — monotonically decreasing in sample count,
  so it is "the luckiest sample," not a central tendency, and it trends BELOW the
  true uncontended latency as samples grow.
- It **understates the true value**: g2p64 sel10% here is min 9.4 ms, but the
  quiet-host `REPORT_payload_m` measured ~21.5 ms for the same cell. The noisier
  host produced a LOWER min — direct evidence that min tracks the best sample, not
  the real warm latency (which sits between this min and the inflated median).
- It **hides intrinsic engine tails** (Qdrant optimizer / segment-parallel
  variance; Postgres buffer/MVCC) that are part of real performance, not noise —
  and it is inconsistent to call the tail "noise" for latency while throughput
  (which IS tail-driven, ~1/mean) is the metric we care about.
- Choosing the metric that most flatters noisy data is motivated reasoning.

So below, latency is shown as **min | median** with both labeled INDICATIVE, and
the verdict does NOT rest on the absolutes. A trustworthy absolute-latency
comparison needs a **quiet host** (co-tenant paused) with **median** as the
primary metric (the quiet-host reports did exactly this, where min ≈ median).

---

## Recall@10 (SOLID) — at matched-recall operating points

| engine / config | sel 1% | sel 2% | sel 5% | sel 10% | sel 20% |
|---|---:|---:|---:|---:|---:|
| acorn g2p64 | 1.00 | 0.99 | 0.99 | 0.99 | 1.00 |
| acorn inline | 0.98 | 0.98 | 0.99 | 0.94 | 0.96 |
| acorn g2p0 | 0.98 | 0.98 | 0.99 | 0.94 | 0.96 |
| pgvector prefilter | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 (exact) |
| Qdrant HNSW | 0.97 | 0.97 | 0.94 | 0.96 | 0.98 |
| **pgvector iterative** | **0.50** | **0.39** | **0.22** | **0.26** | **0.41** |

(acorn/Qdrant at the lowest ef reaching recall>=0.94; iterative = best recall at
ANY ef in 40..800 with strict_order + max_scan_tuples=40000 — it never reaches
0.94.)

**This is the load-independent result and it is unambiguous:** pgvector's
iterative filtered HNSW collapses on the correlated filter; everything else
reaches ~0.94-1.0.

## Verdict (rests on recall + relative behavior, NOT on absolute ms)

1. **pg_acorn beats pgvector-native, decisively.** pgvector's iterative HNSW
   caps at recall 0.22-0.50 at any ef or scan budget — the graph is filter-blind
   and post-filters, so it cannot reach the correlated passing-cluster (see
   "why" below). pgvector's only reliable path is the exact PREFILTER (recall
   1.0), whose cost grows with the sort (its latency rank worsens as selectivity
   rises — bottom of the latency table at sel 5-20% in every measurement,
   contended or not). This is acorn's thesis, and it does not depend on absolute
   ms.
2. **pg_acorn is competitive with Qdrant inside PostgreSQL.** Recall ties
   (0.94-1.0 both). Across the latency runs acorn and Qdrant trade places within
   a small multiple at every selectivity; Qdrant tends to lead at the highest
   selectivity, acorn at low-mid — consistent with the quiet-host
   `REPORT_payload_m` / `REPORT_qdrant_final` (recall tie; acorn faster at sel
   1-5%, Qdrant ~2.5x at sel 20%). The cross-engine GAP is small enough that this
   VM's noise cannot resolve it precisely — which is itself the finding: acorn is
   in Qdrant's league, inside Postgres.
3. **pgvector prefilter is the slow-but-correct baseline** — exact (recall 1.0)
   but its filtered latency degrades fastest with selectivity (large Sort).

## Latency (INDICATIVE — min_ms | median_ms; do not over-read absolutes)

Matched-recall operating point (lowest ef reaching recall>=0.94). Median is
jitter-inflated; min understates; the true value is between them and is best
measured on a quiet host.

| sel | acorn g2p64 | acorn inline | acorn g2p0 | pgv prefilter | Qdrant HNSW |
|----:|---|---|---|---|---|
| 1%  | 2.8 \| 4.7 | 2.1 \| 3.8 | 2.6 \| 4.8 | 3.3 \| 5.2 | 2.6 \| 5.2 |
| 2%  | 3.4 \| 5.2 | 2.8 \| 4.2 | 3.8 \| 6.4 | 6.4 \| 12.8 | 3.3 \| 6.1 |
| 5%  | 5.0 \| 9.4 | 4.7 \| 11.6 | 7.6 \| 43.0 | 13.2 \| 86.3 | 5.1 \| 8.6 |
| 10% | 9.4 \| 70.7 | 5.2 \| 36.4 | 7.0 \| 47.5 | 22.7 \| 141 | 7.5 \| 14.7 |
| 20% | 18.9 \| 156.9 | 14.9 \| 212.6 | 15.3 \| 112.3 | 37.4 \| 215.9 | 12.1 \| 78.8 |

Read this only for the SHAPE: pgvector prefilter is consistently the slowest
filtered path and degrades fastest with selectivity; acorn and Qdrant are in the
same band. The exact ms are not reliable on this host (the g2p64 sel10% column —
min 9.4, median 70.7, quiet-host truth ~21.5 — shows the spread).

## Throughput (INDICATIVE) — peak QPS, multiprocessing concurrency sweep

Concurrency scaling was erratic (8-core + client saturation + VM jitter); peak is
usually @conc=4. Absolute QPS is depressed by the shared VM, Python clients, and
Qdrant's HTTP/JSON path. Relative shape only:

| engine | sel 1% peak | sel 10% peak |
|---|---:|---:|
| pgvector prefilter | 179 (@4) | 25 (@4) |
| Qdrant HNSW | 93 (@4) | 24 (@1) |
| acorn g2p64 | 79 (@4) | 44 (@4) |

(Qdrant also sel5% 64, sel20% 8.) Shape: at low selectivity prefilter wins QPS
(cheap exact sort over few rows, parallelizes well); at mid selectivity acorn
leads (its in-filter graph beats prefilter's large sort and Qdrant's heavier
per-query search). Do not cite the absolute QPS.

## Index size (SOLID) + build time (approx)

| index | size | build time |
|---|---:|---:|
| pgvector hnsw | 199 MB | 5m 42s |
| acorn g2p0 | 249 MB | (prior) |
| acorn g2p64 | 301 MB | 18m 09s (partly contended) |
| acorn inline | 4057 MB | (prior; inline = full vectors) |

acorn graph builds are ~3x slower and larger than pgvector hnsw; the inline
layout is 4 GB (co-located vectors) — a latency play, not a footprint play.

## Why pgvector iterative fails on correlated filters (mechanism)

pgvector's HNSW is built on vector geometry only (filter-blind); the filter is a
POST-CHECK on graph candidates. The correlated fixture derives `bucket` from the
dominant vector block, so passing rows form a vector-space cluster OFF the
query->nearest path. Greedy descent toward the query meets mostly FAILING rows
(99% fail at sel 1%); the true top-k passing rows sit at a large distance-RANK
(many failing rows are nearer). The graph has no edges that preferentially bridge
to the passing cluster, so:
- raising **ef** explores more of the query's neighborhood but not the off-path
  cluster -> recall flat across ef;
- raising **max_scan_tuples** would need to scan down to that large rank ≈ a full
  scan to reach all passing rows; 40000 caught only ~half -> recall ~0.5.

ACORN fixes this at BUILD time: it keeps filter-failing nodes in the candidate
queue (connectivity bridges) and adds payload edges among same-filter nodes, so
the in-filter traversal reaches the passing subgraph directly. That is the entire
recall gap (acorn ~0.99 vs pgvector iterative ~0.2-0.5).

## Methodology pitfalls fixed (both documented in the harness)

- **Qdrant must SETTLE before measuring.** Post-load, the forced-HNSW optimizer
  churns (114% CPU) -> first run gave p95 1100 ms / 12 QPS (garbage). Fix: wait
  for `indexed_vectors_count=250000` + status `green` + CPU idle. Do NOT raise
  `indexing_threshold` to silence the optimizer — that DE-INDEXES segments back
  to exact search (recall 1.0, not HNSW).
- **pgvector iterative needs `strict_order` + raised `max_scan_tuples`** — with
  `relaxed_order` + the default budget, recall collapses further (an unfair
  cripple). Even fairly configured it fails on correlated (the real finding).

## What a legit absolute-latency/throughput re-run requires

Quiet host (pause the co-tenant), warm-up, **median** as the primary latency
(p95 for tail, min only as a labeled floor), and a dedicated throughput rig
(non-Python or pipelined client) to avoid client-bound QPS. The committed
quiet-host `REPORT_payload_m` / `REPORT_qdrant_final` are the trustworthy
absolute references for acorn and Qdrant; this 3-way's durable contribution is
the **deterministic recall comparison** (especially pgvector iterative's
correlated-filter failure) and the **relative ordering**, not the millisecond
absolutes.

## Caveats summary

- Shared 8-core macOS Docker VM + co-tenant -> latency/throughput absolutes
  unreliable; recall + relative ordering are the solid outputs.
- Transport differs (libpq binary vs HTTP/JSON), both native-client end-to-end.
- in-PostgreSQL (MVCC/buffer manager/txn) vs Qdrant in-memory — different
  substrates; acorn being in Qdrant's league inside Postgres is the headline.
- Single correlated (hard) fixture, 250K, dim 128, one host. On a uniform filter
  pgvector iterative fares better and the gaps narrow.
