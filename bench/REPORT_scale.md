# Scaling study: filtered-KNN at 100K / 1M / 10M — pgvector native vs Qdrant vs pg_acorn

Real Cohere wikipedia embeddings (1024-dim), a *hard correlated* payload filter, exact
ground truth, on one dedicated host. Measures **build time + index size**, **recall@10**,
**latency distribution** (median/p95/p99), and **real concurrent throughput** (a
multiprocessing QPS sweep — not a 1/median reciprocal) across three dataset sizes.

> All three scales complete (100K, 1M, 10M). Read the **Caveats** section before
> quoting any single number — the headline is the *scaling trend and the
> recall/latency/throughput trade-offs*, not absolute values on one host.

## What is being compared

| Engine | Filtered-KNN path | Notes |
|---|---|---|
| **pg_acorn** | `acorn_hnsw` index, payload-aware graph | g2p64: m=16, ef_construction=64, acorn_gamma=2, acorn_payload_edges, acorn_payload_m=64, non-inline |
| **pgvector native** | (a) prefilter (bitmap on bucket btree + exact Sort); (b) `iterative_scan=strict_order` over a stock `hnsw` | pgvector's two honest filtered paths; both reported |
| **Qdrant** | forced HNSW + integer payload filter | m=16, ef_construct=64, indexing_threshold=10 (index everything) |

All three hold byte-identical data. pgvector + acorn share the *same* `tv_items` table
(`id, bucket, embedding vector(1024)`); Qdrant holds the same vectors + `bucket` payload.

## Fixture (honest, adversarial)

- **Data**: `Cohere/wikipedia-2023-11-embed-multilingual-v3` (en), 1024-dim, unit-norm,
  cosine. First N rows for N in {100K, 1M, 10M}; 40 held-out rows as queries.
- **Correlated filter**: `bucket` 0–99 derived from each row's *dominant embedding
  block*, so `bucket < sel%` selects a subset **spatially clustered in vector space** —
  the hard case for filtered ANN (a random filter would be much easier). Measured pass
  rates: `<1%`=0.97%, `<5%`=4.83%, `<10%`=9.66%, `<20%`=18.74%.
- **Ground truth**: exact top-10 by cosine among the passing rows, numpy brute force,
  per (query, selectivity).

## Hardware & method

- **Host**: GCP `n2-standard-32` (32 vCPU, 125 GB RAM), 1 TB SSD, asia-northeast1.
- **Clients** run on the same host over localhost TCP for all engines (psycopg → 5432,
  httpx → 6333), via `uv run`. Throughput uses **multiprocessing** (no GIL cap).
- **Build**: m=16, ef_construction=64; `maintenance_work_mem` 4/16/64 GB for 100K/1M/10M;
  `max_parallel_maintenance_workers=30`.
- **Recall/latency**: per engine × selectivity {1,2,5,10,20%}, sweep ef and report the
  **matched-recall** cell — the lowest-ef cell reaching recall ≥ 0.94 (else the
  max-recall cell). pgvector prefilter is exact (recall = 1.0). median/p95/p99 over
  3 reps × 40 queries.
- **Throughput**: at a fixed operating point, concurrency sweep {1,4,8,16,32}; each cell
  is a 6 s steady-state window; QPS = completed queries / wall time. The orchestrator
  drops the other engines' indexes so each is measured in isolation.

---

## Build time + index size vs N

| N | pgvector hnsw | acorn g2p64 |
|---|---|---|
| 100K | 70 s / 0.78 GB | 308 s / 0.82 GB |
| 1M | 164 s / 7.6 GB | 458 s / 8.2 GB |
| 10M | **1500 s (25 m) / 76 GB** | **5201 s (87 m) / 82 GB** |

acorn builds ~1.5–4.5× slower than pgvector (payload-aware edges: up to 96 neighbors/node
vs ~32) and its index is ~8% larger. Neither parallelizes past ~16 maintenance workers —
at 30 workers the 10M build sat at **88% CPU idle**, lock-contention-bound, so more cores
do not help. Build-time improvement levers are tracked in
[docs/build-perf-backlog.md](../docs/build-perf-backlog.md).

## Recall + latency at matched recall (~0.95) — recall \| median \| p95 \| p99 (ms)

### sel = 1%
| N | acorn_g2p64 | pgv_iterative | pgv_prefilter | qdrant |
|---|---|---|---|---|
| 100K | 1.00 \| 1.43 \| 1.72 \| 2.37 | 0.97 \| 25.39 \| 47.74 \| 49.49 | 1.00 \| 5.61 \| 6.11 \| 8.39 | 1.00 \| 1.74 \| 2.02 \| 2.24 |
| 1M | 0.97 \| 23.83 \| 27.79 \| 29.43 | 0.94 \| 59.29 \| 168.69 \| 187.92 | 1.00 \| 58.7 \| 61.47 \| 62.39 | 0.98 \| 2.09 \| 2.4 \| 2.89 |
| 10M | 1.00 \| 33.11 \| 740.36 \| 842.9 | 0.91 \| 166.81 \| 8388.89 \| 15798.01 | 1.00 \| 335.62 \| 345.32 \| 349.52 | 0.99 \| 2.7 \| 3.57 \| 4.45 |

### sel = 10%
| N | acorn_g2p64 | pgv_iterative | pgv_prefilter | qdrant |
|---|---|---|---|---|
| 100K | 0.98 \| 3.39 \| 3.86 \| 4.12 | 0.93 \| 5.92 \| 12.24 \| 13.04 | 1.00 \| 41.42 \| 44.18 \| 44.46 | 0.98 \| 2.17 \| 2.4 \| 2.54 |
| 1M | 0.96 \| 8.39 \| 10.01 \| 10.37 | 0.90 \| 11.49 \| 35.23 \| 36.43 | 1.00 \| 212.96 \| 217.74 \| 219.09 | 0.98 \| 3.88 \| 4.2 \| 4.61 |
| 10M | 0.98 \| 21.86 \| 203.78 \| 265.76 | 0.95 \| 21.92 \| 357.19 \| 1187.83 | 1.00 \| 1941.03 \| 1956.45 \| 1962.7 | 1.00 \| 5.64 \| 6.71 \| 9.32 |

### sel = 20%
| N | acorn_g2p64 | pgv_iterative | pgv_prefilter | qdrant |
|---|---|---|---|---|
| 100K | 0.97 \| 3.46 \| 4.06 \| 4.22 | 0.91 \| 3.12 \| 5.56 \| 7.56 | 1.00 \| 75.03 \| 78.0 \| 78.71 | 0.97 \| 2.29 \| 2.7 \| 2.9 |
| 1M | 0.96 \| 8.79 \| 10.38 \| 10.9 | 0.99 \| 16.63 \| 26.84 \| 29.66 | 1.00 \| 344.91 \| 349.22 \| 353.98 | 0.98 \| 3.82 \| 4.15 \| 4.29 |
| 10M | 0.95 \| 21.38 \| 247.03 \| 443.96 | 0.97 \| 16.26 \| 23.91 \| 24.66 | 1.00 \| 3474.08 \| 3492.84 \| 3503.3 | 0.98 \| 5.87 \| 7.45 \| 9.51 |

## Peak throughput (real QPS) vs N

| N | acorn g2p64 sel10% | pgv prefilter sel10% | qdrant sel10% |
|---|---|---|---|
| 100K | 1765 @32 | 291 @32 | 3054 @32 |
| 1M | 688 @32 | 25 @16 | 906 @32 |
| 10M | **465 @32** | 2 @8 | **118 @8** |

(@conc = concurrency at peak. Throughput is INDICATIVE — see caveat 4.)

---

## Reading of the results

- **acorn scales to 10M with working filtered ANN.** Median latency at matched recall
  stays tens of ms (sel 1% = 33 ms @ r 1.00; sel 10% = 22 ms @ r 0.98; sel 20% = 21 ms @
  r 0.95). The index is genuinely used at every scale (recall rises with ef — e.g. 10M
  sel 1%: ef 40→r 0.59, ef 400→r 1.00).
- **acorn's tail grows at 10M.** p95/p99 inflate badly at low selectivity (10M sel 1%
  p99 = 843 ms vs median 33 ms): on the correlated filter a few queries must traverse far
  to reach the small clustered passing region. This is acorn's clearest weakness at scale.
- **Qdrant is the single-query latency leader at every scale** (10M: 2.7–5.9 ms median
  with tight tails). For raw filtered-KNN latency it is the strongest engine here.
- **pgvector prefilter collapses with N×sel.** Exact sort over the passing set is
  unusable at scale (10M: 336 ms at sel 1% → 1.9 s at sel 10% → 3.5 s at sel 20%;
  throughput 2 QPS).
- **pgvector iterative_scan is selectivity-dependent and tail-unstable.** It can match
  acorn's median at high selectivity (10M sel 20%: 16 ms, r 0.97, tight tail) but is
  catastrophic at low selectivity (10M sel 1%: p99 = 15.8 s, r only 0.91) — the iterative
  re-scan keeps widening on the hard correlated filter.
- **Throughput.** acorn's measured peak QPS exceeds Qdrant's at 10M sel 10% (465 vs 118)
  and is far above pgvector. Read this with caveat 4: Qdrant is driven over HTTP/JSON
  (httpx) and its QPS plateaus by concurrency 8, strongly suggesting a client/transport
  bound rather than a server limit — so this is **not** evidence that acorn's engine is
  faster than Qdrant's, only that the in-process libpq path sustains more load in this
  rig. Single-query latency (above) is the fairer head-to-head, and there Qdrant leads.

**Bottom line.** Against pgvector's own filtered paths, acorn is the clear win at scale —
it stays in the tens-of-ms band where prefilter hits seconds and iterative_scan's tail
explodes. Against Qdrant, acorn is competitive on median latency (within ~4–10× at 10M)
and holds up on sustained load, but Qdrant wins single-query latency and tail behavior,
and acorn's low-selectivity tail on a correlated filter is the gap to close.

## Caveats (read these)

1. **Matched recall, not matched ef.** Engines are compared at the lowest ef reaching
   recall ≥ 0.94. pgvector prefilter is exact (recall 1.0) by construction.
2. **The fixture is deliberately hard.** `bucket` correlates with vector position, so a
   low-selectivity filter yields a small, spatially clustered candidate set — the worst
   case for filtered HNSW, and the source of acorn's heavy low-sel tail. A random filter
   (common in practice) is much easier and would narrow the gap.
3. **Storage/transport are not equal.** acorn/pgvector run *inside PostgreSQL* (MVCC,
   shared-buffer cache, libpq, the planner; vectors TOASTed at 1024-dim); Qdrant is a
   purpose-built in-memory engine over HTTP/JSON. Absolute latency is not apples-to-apples.
4. **Throughput is INDICATIVE and transport-bound.** Client and server share the same 32
   cores. The Qdrant client is HTTP/JSON (httpx) and its QPS flattens by concurrency 8 —
   a client/transport ceiling, not a server limit; gRPC would raise it. So the acorn>Qdrant
   QPS at 10M is a rig artifact, not an engine verdict. Use the **shape** (rises then
   plateaus) and the pgvector comparison, not the absolute acorn-vs-Qdrant ceiling.
5. **Build does not scale past ~16 cores** (measured 88% idle at 30 workers) — more vCPUs
   do not shorten it. Build times are a floor for this index class; see
   [docs/build-perf-backlog.md](../docs/build-perf-backlog.md).
6. **Scope.** One host, one fixture family, 40 queries, dim 1024. Treat absolute numbers
   as indicative; the **scaling trends and the recall/latency trade-offs** are the
   takeaway. p99/min are extreme order statistics — read median/p95 as primary.

## Reproduce

```
# on the VM (data prepared by cohere_prep.py -> ~/scale_data)
bash bench/run_scale.sh 100000   4GB  0
bash bench/run_scale.sh 1000000  16GB 0
bash bench/run_scale.sh 10000000 64GB 1     # STOPQ=1: stop Qdrant during PG builds
python3 bench/scale_report.py --dir ~/scale_data --ns 100000,1000000,10000000 --builds '{...}'
```

Harness: `cohere_prep.py` (fixture), `scale_load.py` (binary COPY load + Qdrant upload +
exact truth), `scalebench.py` (latency + throughput), `run_scale.sh` (per-scale build +
measure), `scale_report.py` (tables). Results: `bench/results_scale_{100000,1000000,10000000}.json`.
