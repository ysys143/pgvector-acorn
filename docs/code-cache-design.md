# Design: Per-Index Shared-Memory SQ8 Code Cache (Phase C / C-1)

Status: M1 (read) + M2 (write) IMPLEMENTED and merged to main (2026-06-13);
cache defaults OFF pending M3. Prereq reading: `bench/REPORT_index_size.md`.

## 1. Problem

The inline layout buys its speed by bypassing PostgreSQL's per-neighbor
buffer access (~20 us/access measured); the price is one SQ8 copy of every
node per in-edge — 64x duplication, a 4,057 MB index at 250K (20.5x stock
pgvector, 16.3x the non-inline acorn layout). Non-inline (249 MB) loses the
competitive claim outright (790 ms vs prefilter 225 ms at sel=10%,
recall 0.99). Deduplicating codes into regular index pages would not help:
each neighbor lookup would still pin a page and pay the same ~20 us.

The fix must (a) store each code once and (b) make the per-neighbor lookup
not go through the buffer manager.

Precedent: DiskANN keeps compressed (PQ) codes for ALL nodes in RAM and the
graph + full vectors on disk. Translated to PostgreSQL: SQ8 codes for all
elements live in a per-index shared-memory table; index pages carry only
TIDs (the existing non-inline layout).

## 2. Goals / Non-goals

Goals (acceptance gates in section 9):

- G1 size: acorn index <= 1.3x stock pgvector at 250K (non-inline measures 1.26x).
- G2 speed: money cells (sel=1% ef=200, sel=10% ef=800, sel=20% ef=800)
  <= 1.3x inline medians on a quiet host.
- G3 recall: parity with inline under the direction-aware gate (same quantizer).
- G4 correctness: results never depend on cache state — any miss or
  inconsistency degrades to the non-inline read path, never to wrong output.
- No on-disk format change, no WAL change (element tuples already persist
  everything the cache needs).

Non-goals (v1):

- PG < 17 (the design uses the PG17 DSM registry).
- Cache persistence across postmaster restarts (rebuilt lazily on first scan).
- Per-entry eviction (admission and eviction are whole-index).
- Tier 1 (hook) path — this is a Tier 2 (`acorn_hnsw`) feature.

## 3. Architecture

### 3.1 On-disk: unchanged non-inline layout

Indexes are built/maintained exactly as `acorn_inline_vectors=off` today.
The element tuple (`AcornT2ElementTupleData`) already stores fp32 vector,
`filter_val`, heaptids, `neighbortid`, and level — it is the persistent,
WAL-logged source of truth from which the cache is (re)built. The 16.3x size
reduction therefore comes for free, and crash safety is inherited.

### 3.2 Shared memory: one code table per index

Entry (mirrors the inline entry minus the per-edge `indextid`):

    { heaptid, nbrtid, level, flags, filter_val, scale, offset, code[dim] }

40 B header + dim bytes; 168 B at dim=128 -> ~42 MB per 250K elements plus
map overhead (~50 MB total).

Registry: `GetNamedDSMSegment("pg_acorn_cc")` (PG17 dsm_registry) holds a
small directory: an LWLock plus an array of per-index slots keyed by
`(dboid, relfilenumber)`. Each slot owns a DSA area containing:

- header: state `EMPTY | LOADING | READY | PARTIAL`, generation counter,
  nelems, bytes, lastused;
- a dshash keyed by the element TID packed into uint64
  (`blkno << 16 | offno`) mapping to the DSA pointer of the entry.

Keying by `relfilenumber` makes REINDEX/TRUNCATE naturally start a fresh
table; orphans are reclaimed by eviction (3.5).

### 3.3 Loader (lazy, non-blocking)

First scan that finds the slot `EMPTY` CASes it to `LOADING` and walks the
index main fork sequentially: for every element tuple, quantize fp32 -> SQ8
with the same per-vector scale/offset quantizer the inline build uses
(extracted into a shared `acorn_sq8_encode()` so codes are bit-identical to
inline's) and insert the entry. ~32K pages at 250K -> sub-second warm,
low single-digit seconds cold. While `LOADING`, other scans (and the loading
scan itself for nodes not yet cached) use the fallback path — nobody waits.
On completion the slot flips to `READY`. If the DSA hits the memory cap
mid-load the slot becomes `PARTIAL`: present entries serve, misses fall back.

### 3.4 Scan integration (`acorn_scan.c`)

At neighbor discovery, where the inline path reads an `AcornT2InlineEntry`
from the neighbor-tuple chain, the cache path does one dshash lookup:

- hit: evaluate `filter_val` against the ScanKey, compute SQ8 distance,
  push candidate; at expansion reuse `nbrtid`/`level`; at emission reuse
  `heaptid`. Identical semantics to the inline entry.
- miss (or slot not `READY`/`PARTIAL`-miss): existing non-inline element-page
  read. Correctness never depends on the cache (G4); a miss costs the
  non-inline ~20 us for that edge only.

Multi-heaptid elements (duplicate vectors): the entry stores heaptids[0],
as the inline entry does today; the remaining TIDs are recovered through the
fallback read at emission, matching inline behavior.

### 3.5 Memory management

- GUC `pg_acorn.code_cache_size` (default 512MB, 0 = feature off).
- Admission is whole-index: if the projected table (nelems x entry size)
  does not fit beside resident tables, evict least-recently-used tables
  (whole-table granularity); if it still does not fit, stay `EMPTY` and
  WARN once per backend. Oversized indexes simply run at non-inline speed.
- `pg_acorn_code_cache_stats()` SRF and `pg_acorn_code_cache_evict(regclass)`
  admin function for observability and manual control.

## 4. Consistency protocol

Invariant: the cache is a hint, never an authority (G4).

- Element vectors are immutable once inserted (HNSW property); entries are
  immutable except `flags`.
- INSERT (`aminsert`): after the element tuple and neighbor updates are
  written (exactly as today), upsert the cache entry. An aborted transaction
  leaves a dead element that scans already tolerate; its cache entry is
  removed when VACUUM removes the element. A crash between tuple write and
  cache upsert just leaves a miss.
- DELETE/VACUUM (`ambulkdelete`): for each element removed, delete its cache
  entry under the slot's generation; TID reuse by a later insert overwrites
  via upsert.
- Torn reads: each entry carries a 32-bit version (seqlock discipline: odd =
  being written). A reader seeing an odd or changed version retries once,
  then falls back to the element-page read.
- Hot standby: read-only loader works identically (reads pages, builds local
  shared table on the standby); no WAL interaction by construction.

## 5. GUCs and reloptions

| Name | Default | Meaning |
|---|---|---|
| `pg_acorn.code_cache_size` | 512MB | total shared budget; 0 disables |
| `pg_acorn.scan_code_cache` | on | per-session kill switch (debug/A-B) |
| `acorn_inline_vectors` (reloption) | off (flip after gates pass) | inline indexes never consult the cache; orthogonal |

## 6. Performance model

Per discovered neighbor: packed-TID dshash lookup (~150-300 ns, read-mostly,
partitioned locks) + entry dereference, vs the inline path's in-page array
access (~50-150 ns). At sel=10% ef=800 (~25K discoveries) the delta is
+5-8 ms on inline's 72.8 ms -> ~1.1x, within the 1.3x gate. The structural
wins over non-inline: 36,849 page accesses/query collapse to the expansion
set only (~1,800), and the 249 MB index plus 50 MB cache fit comfortably
inside `shared_buffers` where the 4 GB inline index could not — expect the
gap vs inline to NARROW at n >= 1M where inline thrashes.

## 7. Failure modes

| Event | Behavior |
|---|---|
| postmaster restart | slots empty; first scan reloads (lazy) |
| DSA OOM mid-load | slot `PARTIAL`; misses fall back; WARN |
| concurrent insert during load | upsert and loader write identical bytes; idempotent under entry version |
| REINDEX / new relfilenumber | new slot; old one ages out via eviction |
| cache disabled (`=0` / kill switch) | exact non-inline behavior |

## 8. Milestones

- M1 read path: registry + loader + scan lookup + fallback, behind
  `scan_code_cache` (default off). Exit: G2 measured on the 250K A/B rig
  (`bench/noinline_ab.py` extended with a `cache` mode).
- M2 write path: `aminsert` upsert, `ambulkdelete` invalidation, entry
  versioning, TID-reuse test, insert-during-scan test.
- M3 budget/eviction, stats SRF, docker-test integration, docs; flip
  `acorn_inline_vectors` default to off; acceptance re-run (G1-G5).
- G5 memory: Z4 RSS harness shows shmem bounded by the GUC and no
  backend-local growth across 10K-query soak.

## 9. Alternatives rejected (measured rationale)

- C-2 SQ4 + slimmer inline entries: per-edge duplication floor is ~7-10x
  stock at gamma=2 (page granularity dominates after the diet); recall risk
  from 4-bit codes; does not remove the scaling blocker.
- C-3 partial inline (subset of slots): proportional blend of C-2's size and
  non-inline's latency; worst of both at the money cells.
- C-4 non-inline default: refuted by measurement (790 ms vs prefilter
  225 ms at sel=10% recall 0.99).
- Persistent "code fork"/sidecar pages without shmem: still one buffer
  access per neighbor (~20 us) -> non-inline speed; adds format + WAL
  surface for no latency win.

## 10. Field stability: the ef=1600 cache crash (root-caused 2026-06-13)

Symptom during the 250K G2 A/B runs: a backend in cache mode at sel=1%
ef=1600 died with `exited with exit code 2` and no preceding ERROR/PANIC,
forcing a postmaster crash-restart. Observed twice, both under heavy host
contention (a co-tenant container, ezis, pinned at ~267% CPU; the same runs
show p90 latencies 30-60x their medians and the postmaster issuing SIGKILL
to "recalcitrant children").

Root cause: NOT a defect in the code-cache path; environmental
(host-contention-induced sibling-process death on the shared Docker VM).
Evidence:

- `exited with exit code 2` from a PG17 backend is reachable only via
  `quickdie()` — the SIGQUIT handler. A backend exits 2 because the
  postmaster broadcast SIGQUIT after ANOTHER process died, never from a
  segfault in the exiting backend itself. No `signal 11`/`signal 6` (the
  signatures a real cache-path memory bug would produce) was ever logged.
- Controlled quiet-host repro (`bench/cc_ef1600_repro.py`, ezis paused):
  600 ef=1600 cache queries at 250K across sel 1/10/20% — zero crashes,
  rig healthy. The M1.5 60K hammer (300 ef=1600 cache queries) was likewise
  clean.
- Memory headroom: the VM has 9.69 GB usable; co-tenant Postgres containers
  commit ~4.2 GB of `shared_buffers` alone (bench 2 GB + z4 2 GB +
  pgvec 128 MB) before any query work, so contention-driven memory pressure
  is a credible trigger for an OOM kill of one backend whose SIGQUIT
  collateral then reads as the observed exit-2 on the others.
- M2 additionally removed M1's only dangling-pointer risk (the lookup that
  returned a direct shared-memory pointer) by switching to seqlock copy-out,
  so even a latent torn-read path no longer exists.

Operating guidance: ef<=800 is unaffected; ef=1600 cache is stable given
adequate host headroom. A definitive contention-repro (deliberately
overcommitting the VM until exit-2 recurs) was NOT run — it would crash the
rig and the co-tenant project repeatedly for no additional design signal.
