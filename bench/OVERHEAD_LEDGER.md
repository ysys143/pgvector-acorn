# pg_acorn Overhead Ledger — every microsecond of the Qdrant gap, attributed

Workload: n=30,000, dim=128, synthetic correlated (`correlation=high`, seed 42 —
bit-identical to `bench/fixtures/synthetic.py`), hot cache, single client,
shared idle Docker stack (postgres + qdrant v1.16, same VM). k=10.
Measurement scripts: `bench/overhead_ledger.py` (S1-S5),
`bench/ledger_reconcile.py`, `bench/ledger_param_probe.py`,
`bench/ledger_tail_probe.py`. Raw numbers: `bench/results_overhead_ledger.json`.

Nominal "sel=1/10/40" thresholds have actual selectivity 8.35% / 16.7% / 41.75%
on the high-correlation generator (buckets are multiples of 8; `bucket < 1`
selects one 10-dim dominant block out of 12).

## 0. Reconciliation: most of the reported 4x gap was the measurement path, not the engine

The prior benchmark reported PG bitmap-prefilter at 79-124 QPS vs Qdrant 403
QPS (4x) at threshold=1. Re-running the EXACT prior query path
(`bench/ledger_reconcile.py`: psycopg unprepared, `q.tolist()` parameter,
QPS = N/sum(latencies)) on the now-idle stack reproduces 75.1 QPS
(mean 13.31 ms, p99 105.6 ms) — while the median of the same queries is
2.0-2.9 ms (`bench/ledger_param_probe.py`, all four parameter variants).

`bench/ledger_tail_probe.py` (300 iterations, EXPLAIN-paired):

| engine | median | mean | p99 | spikes >5x median | N/sum QPS | median QPS |
|---|---|---|---|---|---|---|
| PG (bitmap KNN, th=1) | 2.81 ms | 8.03 | 176.5 | 14/300 (6/300 also spike server-side, worst 221 ms exec) | 124.5 | 356 |
| Qdrant (same filter, hnsw_ef=128) | 1.87 ms | 6.04 | 60.8 | 18/300 | 165.6 | 535 |

The latency distribution on this Docker-for-Mac VM has a fat environmental
tail that hits BOTH engines. `QPS = N/sum(latencies)` is governed by that
tail; the prior per-point QPS therefore bounced 2-4x between identical
work-loads (e.g. prior tier2_g4 th=40: ef=200 -> 83.9 QPS, ef=400 -> 203.1 QPS
with IDENTICAL 2672.65 buffers/query), and picking each engine's best point
across the ef sweep awarded Qdrant its luckiest sample. Verdict on the prior
operating points:

- "PG 89-124 vs Qdrant 403 QPS (4x)" -> median-basis gap is 2.3-2.8 ms vs
  1.9 ms = **1.2-1.5x**, fully attributed below.
- "acorn 203 QPS vs Qdrant 369 (1.8x)" -> median-basis: acorn ef=10 is
  **1.29 ms/q (775 QPS) at recall 0.953 — FASTER than Qdrant's 2.0-2.3 ms** at
  recall 1.0. The real gap at threshold=40 is recall (0.953 plateau, a graph
  -build ceiling), not speed.
- Remaining honest engine-side deficits: per-unit-of-work costs in section 2.

Benchmark-methodology consequences (no C code): report medians + p99
separately, use prepared statements, pass pre-formatted vector literals, and
never aggregate QPS across a fat-tailed run. These alone "recover" ~3-5x of
reported QPS.

## 1. Measured unit costs (the price list)

| unit cost | value | how measured |
|---|---|---|
| distance, SQL/fmgr path (`<=>`, 128-dim) | **132.9 ns** | full-pass `sum(embedding <=> q)` minus `count(*)` baseline, /30000 (S1) |
| distance, numpy float32 SIMD floor, same container | **28.3 ns** | batched matvec, OPENBLAS_NUM_THREADS=1 (S1) |
| => fmgr + detoast + executor tax on one distance | **4.7x (104.6 ns/call)** | ratio |
| buffer access, acorn index scan (hot, pin+lock+release) | **0.75-0.82 us** | regression of exec_ms vs buffers across per-query ef sweep, 0.5*t_dist removed (S4) |
| buffer access, bitmap heap scan | ~0.75-1.78 us | S2 least squares th=1..49 (R2=0.94; collinear with tuple cost — bracketed) |
| heap tuple visibility + deform (vector col) | **0.32 us** | S2 refit excluding th=100 outlier |
| executor cost per emitted index tuple (amgettuple cycle + heap fetch + projection) | **4.7 us/row** | S4 LIMIT 10 vs 100 probe: +0.42 ms / +90 rows |
| PG protocol + parse/plan per query (prepared / unprepared) | 0.2-0.3 / 0.4-0.5 ms | client minus EXPLAIN exec (S3) |
| Qdrant HTTP+JSON per query (this client) | ~2.2 ms intercept | hnsw_ef sweep intercept (S5) — Qdrant is protocol-bound at this scale |
| Qdrant marginal cost per hnsw_ef unit (th=40, graph path) | **0.44 us** | sweep slope (S5) |
| Qdrant th=1: latency flat in hnsw_ef (slope ~0) | — | consistent with its planner taking the plain (exact, payload-index) path below `full_scan_threshold`; treat its "graph path" status at this selectivity/scale as unverified |

OpenBLAS in the bench container spin-waits pathologically multi-threaded
(150 ms for a 30000x128 matvec vs 0.46 ms single-threaded) — the numpy floor
is measured pinned to 1 thread.

## 2. The ledger

### [A] threshold=1 (8.35%, 2504 passing) — bitmap prefilter + exact sort, recall 1.0

Plan: Limit -> Sort(top-10) -> BitmapHeapScan -> BitmapIndexScan(btree).
Median exec 2.45 ms (EXPLAIN-instrumented), client 2.33 ms prepared / 2.57 ms
unprepared. Qdrant same filter: 1.87-2.32 ms client (recall 1.0).

| component | unit cost | count/query | ms/query | share of exec |
|---|---|---|---|---|
| buffer manager, heap+bitmap pages | 0.75 us | 1509 | **1.13** | 46% |
| heap tuple visibility + deform | 0.32 us | 2504 | **0.80** | 33% |
| distance eval (fmgr, in BitmapHeapScan tlist) | 132.9 ns | 2504 | **0.33** | 14% |
| Sort node (top-10 heapsort, excl.) | — | 2504 in | 0.27 | 11% |
| Bitmap Index Scan (btree) | — | 2504 tids | 0.16 | 7% |
| planner | — | 1 | 0.06 | 2% |
| sum of components vs measured exec | | | 2.75 vs 2.45 | closes within ~12% (instrumentation inflation) |
| + protocol/parse (client, prepared) | | | ~0.2-0.3 | |

Cross-check: BitmapHeapScan exclusive time 1.98 ms vs modeled
1509x0.75 + 2504x0.32 + 2504x0.133 = 1.93 ms — **97% closure**.

Gap accounting vs Qdrant (1.87 ms median, ~0.2-0.6 ms of it engine-side after
its ~1.6-2.2 ms HTTP intercept): PG's engine-side excess is ~1.6-2.0 ms =
buffer manager (1.13) + tuple access (0.80) + fmgr distance tax (0.25 of
0.33). This matches the SIGMOD'26 in-PG study: buffer management = 55.9-84.4%
of cycles, heaptid fetch 60-75% [1]. Mine: (1.13+0.80)/2.45 = 79%.

### [B] threshold=40 (41.75%) — acorn tier2 gamma=4, ef=10, recall 0.953

Plan: Index Scan (acorn_hnsw, forced). Median exec 0.99 ms, client 1.29 ms.
Buffers 1226/query; model expansions E=10, discovered neighbors
D=(1226-2E-10)/2=598. Qdrant: 2.01-2.82 ms client, recall 1.0.

| component | unit cost | count/query | ms/query | share of exec |
|---|---|---|---|---|
| buffer manager (2 accesses per discovered neighbor + 2/expansion + heap fetches) | 0.75 us | 1226 | **0.92** | 93% |
| distance eval (fmgr, FunctionCall2 in AM) | 132.9 ns | 598 | 0.08 | 8% |
| heap fetch + executor per emitted row | 4.7 us | 10 | 0.05 | 5% |
| scan bookkeeping (visited hash, pairingheap, palloc) | residual | — | **-0.03** | -3% |
| total model vs measured | | | 1.02 vs 0.99 | **residual -3%** (<30% requirement met) |
| + protocol/parse (client - exec) | | | 0.29 | |

The floor model fully explains the scan; no unattributed "Postgres overhead"
remains. Per discovered neighbor pg_acorn pays 1.66 us (= 2 buffer accesses
1.50 us + fmgr distance 0.13 us + bookkeeping); Qdrant pays ~0.44 us per
hnsw_ef unit. **The honest per-unit-of-work engine gap is ~3.8x, and ~90% of
it is the buffer manager double-hit per neighbor, not distance math.**

Key code fact (src/acorn_scan.c, `acorn_stream_expand`): each unvisited
neighbor costs TWO buffer pin/lock cycles — `acorn_distance()` reads the
element page, then `acorn_load_element()` re-reads the SAME page for
heaptid/deleted. That is ~598 redundant buffer accesses = ~0.45 ms/query =
45% of exec time, the single largest attributable line in the acorn path.

### Recall, not speed, is the th=40 gap
acorn recall plateaus at 0.953-0.957 from ef=10 to ef=320 (buffers saturate at
2536 — natural termination). Qdrant delivers 1.0 at the same latency class.
PG reaches recall 1.0 only via the exact bitmap path: est.
2154x0.75 + 12526x0.452 + sort ~= 8.3 ms (prior measured 23.5 QPS under tail).
This is the graph-build/connectivity issue (separate track), not overhead.

## 3. Bypass routes (researched, sized with THIS ledger's numbers)

| # | route | mechanism | expected gain (this workload) | effort | risk |
|---|---|---|---|---|---|
| 1 | Measurement/report fixes: medians + prepared + literal params | no code, bench only | reported gap shrinks 3-5x; [A] 79 QPS -> 356 median QPS | S | none |
| 2 | Neighbor double-load dedup in `acorn_stream_expand` (read element page once: distance + heaptid + deleted in one pin) | C change in scan loop | [B] -598 buffer accesses = -0.45 ms -> exec 0.55 ms, client ~0.85 ms (**~1170 QPS, 1.5x**) | M | low (same locking discipline) |
| 3 | fmgr bypass / batched distance (in progress by another agent) | direct C call or per-batch fmgr | ceiling from S1: 104.6 ns/call x 598 = **0.06 ms in [B] (6%)**, x 2504 = **0.26 ms in [A] (11%)**. Real but NOT the lever; "Batching in executor" thread reports 30-40% only when whole qual/agg pipeline is batched [4] | S | low |
| 4 | Shared-memory graph topology + quantized-vector cache (DSA, built on first scan; hops bypass bufmgr) | like Neon pg_embedding's original in-memory HNSW [5]; pgvectorscale instead colocates neighbors+quantized vecs per node page (DiskANN layout) to make it 1 buffer access PER HOP rather than per neighbor [2][3] | [B] buffer line 0.92 -> ~0.05-0.1 ms; exec ~0.2 ms; client bound by protocol ~0.5 ms (**~2000 QPS, ~2.6x**) | L | high: invalidation on insert/vacuum, crash-safety, memory accounting — the reasons Neon moved pg_embedding back on-disk [5] |
| 5 | halfvec / SBQ quantization (dim=128) | pages halve for heap path; SBQ = 1-2 bit/dim z-score encoding (pgvectorscale) [2] | [A]: 1509 -> ~830 pages = -0.5 ms (exec 2.45 -> ~1.9 ms); [B]: little — the in-PG study measured **no consistent QPS gain from quantization for graph search inside PG** because random page access dominates [1]; only pays combined with #4's layout change | M | recall (needs rerank); halfvec needs opclass plumbing |
| 6 | Index-only emission (amcanreturn + visibility map) | skip heap fetch for all-visible pages; bucket is already IN the index (int4_acorn_ops) | [B]: emitted rows are only 10-100/query -> 4.7 us x rows = 0.05-0.47 ms; meaningful at K=100+, marginal at K=10 | M | VM staleness -> heap recheck path still needed |
| 7 | PrefetchBuffer / PG17 ReadStream for scan I/O | async read-ahead of candidate pages | **0 on this workload** (pages_read=0, all hits — prefetch does not reduce pin/lock CPU); becomes relevant when index > shared_buffers; index AM ReadStream/amgetbatch work is targeted at PG19 [6] | M | none on hot path |
| 8 | amcanparallel / parallel graph search | parallel workers share frontier | not viable for ordered amgettuple scans today; pgvector tracks it as open work + LWLock contention issue #766 [7]; bitmap exact path already parallelizes natively at larger n | L | high |

### Top-3 by expected-gain/effort

1. **#2 neighbor double-load dedup** (M): [B] 1.29 -> ~0.85 ms/q => ~1170 QPS
   at recall 0.953 — ~2.4x faster than Qdrant's median on the same filter.
2. **#1 measurement fixes** (S): immediately corrects the headline numbers;
   [A] "89-124 QPS" becomes 356-430 QPS median (prepared), gap vs Qdrant
   1.2-1.5x.
3. **#4 shmem/topology-colocated graph** (L, do AFTER #2): [B] -> ~0.5 ms/q
   (~2000 QPS); this is the pgvectorscale existence proof that a Postgres
   extension can beat dedicated engines (28x lower p95 vs Pinecone s1 at 99%
   recall, 50M x 768d) [3].

### Projected honest ceiling (median, this stack, recall >= 0.95)

| selectivity | PG today (median) | PG after #2+#3 | PG after all (#2-#6) | Qdrant median |
|---|---|---|---|---|
| 8.35% (th=1, exact) | 2.33-2.57 ms (390-430 QPS) | 2.0 ms (500 QPS) | ~1.4 ms (**~700 QPS**) | 1.87 ms (535 QPS) |
| 16.7% (th=10, exact, est.) | ~4.4 ms est (~230 QPS) | ~3.8 ms | ~2.4 ms (**~420 QPS**) | ~2.0 ms (~500 QPS) |
| 41.75% (th=40, acorn @0.953) | 1.29 ms (775 QPS) | 0.85 ms (1170 QPS) | ~0.5 ms (**~2000 QPS**) | 2.0-2.3 ms (430-500 QPS) |

Caveats: n=30K hot-cache and single client; the buffer-manager share (and
routes #4/#5/#7) grows with n — the [B] residual analysis must be repeated at
200K+ before extrapolating; recall at th=40 stays capped at ~0.955 until the
graph-build (2-hop / gamma) track lands.

## Sources

[1] An In-Depth Study of Filter-Agnostic Vector Search on a PostgreSQL
    Database System (SIGMOD/PACMMOD 2026), arXiv:2603.23710 — buffer
    management 55.9-84.4% of CPU cycles; heaptid fetch 60-75% of cycles;
    translation-map mitigation 8-17%; quantization yields no consistent QPS
    gain for graph search inside PG; PG up to 10x slower than HNSWLib on
    identical workloads.
[2] Tiger Data / Timescale, "How we made PostgreSQL as fast as Pinecone" —
    SBQ: z-score 1-2 bit/dim (2-bit under 900 dims; 96.5% -> 98.6% recall);
    StreamingDiskANN on-disk layout + streaming get_next() filtering.
[3] Tiger Data, "Pgvector is now faster than Pinecone at 75% less cost" —
    50M Cohere 768d: 28x lower p95 / 16x QPS vs Pinecone s1 @99% recall;
    1.4x / 1.5x vs p2 @90% recall.
[4] pgsql-hackers, "Batching in executor" — fmgr per batch instead of per row:
    ~30-40% on scan+qual+agg pipelines.
[5] Neon pg_embedding — original HNSW built/kept in memory (with OOM guard),
    later rewritten on-disk for durability; extension since deprecated.
[6] pgsql-hackers, "index prefetching" / amgetbatch (Vondra et al.) — batch-
    oriented index AM interface + ReadStream, targeted PG19; no built-in
    index AM uses ReadStream as of PG17.
[7] pgvector issue #766 (LWLock contention in concurrent HNSW scans), #359
    (contribution ideas: parallel index scan branch).
[8] Qdrant docs/blog — filterable HNSW with payload-aware extra links;
    payload-index cardinality estimation + full_scan_threshold plain-search
    fallback; ACORN search param added in v1.16 (2-10x slower, for highly
    selective multi-filter cases).
