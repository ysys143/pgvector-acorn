# About pg_acorn

**pg_acorn** is a PostgreSQL extension that brings filtered HNSW search to pgvector without recall ceiling or expensive post-filter overhead.

## The One-Liner

Filterable HNSW for PostgreSQL — filtered search on categorical columns while maintaining recall >0.95 at 1–40% selectivity, where pgvector's post-filter strategy hits 0.7–0.85 ceiling.

## Core Problem Solved

When you query pgvector's HNSW with a WHERE clause:

```sql
SELECT * FROM items 
  WHERE category = 'shoes'        -- pgvector planner: ignore index, do seq scan
  ORDER BY embedding <=> query 
  LIMIT 10;
```

pgvector's HNSW + post-filter workaround forces you to:
- Either ignore the index (seq scan, slow)
- Or scan 1000s of neighbors to return 10 (expensive, low recall)

pg_acorn preserves graph connectivity under filters — you get true top-k with no recall penalty.

## Two Deployment Modes

1. **Tier 1 (Zero Migration)**: Install the extension. Existing `USING hnsw` indexes automatically gain ACORN filtering via a planner hook. No index rebuild, no SQL changes.

2. **Tier 2 (Full ACORN)**: Use the dedicated `USING acorn_hnsw` access method with predicate-aware cost estimation and optional vector co-location for sub-millisecond latency.

## Why It Matters

| Selectivity | pgvector HNSW Recall | pg_acorn Recall | Latency Improvement |
|-------------|---------------------|-----------------|---------------------|
| 1% | 0.75 | **0.96** | 2–5× faster |
| 5% | 0.82 | **0.96** | 2–3× faster |
| 10% | 0.88 | **0.96** | Similar |
| 40% | 0.90 | **0.96** | Similar |

At 1–5% selectivity, pg_acorn eliminates the recall ceiling and speeds up search. At 10–40%, it maintains recall without recall degradation.

## When to Use pg_acorn

✅ **Good fit:**
- Medium–high selectivity (1–40%) with categorical filters
- Correlated embeddings (category-specific vectors)
- Need guaranteed top-k under filters
- Want to avoid index rebuild (Tier 1)

❌ **Not a good fit:**
- Selectivity <1% (post-filter already cheap)
- Selectivity >50% (pgvector post-filter dominates)
- 50M+ rows, memory-constrained (use compression instead)
- Arbitrary WHERE expressions (only categorical columns supported)

## Technical Details

Built on the [ACORN algorithm](https://arxiv.org/abs/2403.04871), which:
- Preserves graph connectivity under filters by keeping filter-failing nodes in the traversal candidate queue (but excluding from results)
- Guarantees top-k regardless of filter selectivity
- Widely shipped: Qdrant 1.16, Elastic/Lucene, Weaviate

pg_acorn brings the same to PostgreSQL with two index types and optional vector co-location for sub-ms latency.

## Key Features

- **Zero-migration Tier 1**: No index rebuild, no SQL changes — install and existing indexes get ACORN filtering
- **Predicate-aware search**: Learn filter-value distribution at build time, guide search at query time
- **Recall preservation**: Avoid 0.7–0.85 ceiling at 10–40% selectivity
- **Vector co-location** (Tier 2): Optional SQ8-quantized vectors inline in index for 10× faster scan
- **Same SQL interface**: No query rewrites; uses pgvector's `<=>` operator and cost estimation

## Installation

```bash
make && make install
psql -c "CREATE EXTENSION pg_acorn;"
```

Requires: PostgreSQL 16+, pgvector 0.8.x+

## Benchmarks

See [bench/](bench/) for reproducible harnesses (pgvector + Qdrant + pg_acorn); the consolidated verdict is [bench/COMPETITIVE_VERDICT.md](bench/COMPETITIVE_VERDICT.md).

## Documentation

- [README.md](README.md) — Quick start, index types, tuning
- [docs/architecture.md](docs/architecture.md) — Tier 1 / Tier 2 design
- [bench/COMPETITIVE_VERDICT.md](bench/COMPETITIVE_VERDICT.md) — acorn vs pgvector vs Qdrant (SSOT)
- [docs/project-log.md](docs/project-log.md) — experiment ledger (what was tried / results / open)
- [docs/development-roadmap.md](docs/development-roadmap.md) — roadmap (1.0 gated by stabilization)

## Status

**Tier 1** (zero-migration hook + CustomScan): Implemented, tested, benchmarked.
**Tier 2** (acorn_hnsw AM): Implemented with predicate encoding, gamma support, payload edges, inline vectors.

All features stable for PostgreSQL 16+.

## Related Work

- **pgvector** — https://github.com/pgvector/pgvector (built on top)
- **ACORN paper** — https://arxiv.org/abs/2403.04871 (theoretical foundation)
- **Qdrant** — Shipped ACORN in production (Qdrant 1.16)
- **Elastic** — Filterable HNSW in Elasticsearch
- **Weaviate** — Integrated ACORN in production vector database

---

**License**: Same as pgvector. See [LICENSE](LICENSE).
