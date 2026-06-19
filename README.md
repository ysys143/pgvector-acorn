# pg_acorn

Filterable HNSW for PostgreSQL — filtered search without recall ceiling or expensive post-filter cost.

**Built on** [pgvector](https://github.com/pgvector/pgvector) and [ACORN](https://arxiv.org/abs/2403.04871) approximate nearest neighbor filtering.

## What it is

pg_acorn is **not** a replacement for pgvector. It is an improved HNSW index type that enables efficient filtered similarity search. SQL syntax, transaction semantics, MVCC, and planner decisions remain entirely within PostgreSQL.

Two deployment modes:

- **Tier 1 — Zero migration**: Install the extension. Existing `USING hnsw` indexes gain ACORN-1 filtered search via a planner hook + CustomScan. No index rebuild needed.
- **Tier 2 — Full ACORN-gamma**: A dedicated `acorn_hnsw` index access method with M×gamma neighbor storage at build time, predicate-aware cost estimation, and optional vector co-location for sub-millisecond filtered search.

```sql
-- SQL unchanged. No query rewrites needed.
SELECT * FROM items 
  WHERE category = 'shoes'
  ORDER BY embedding <=> $1 
  LIMIT 10;
```

The index learns which categories appear in which neighborhoods during build, then uses that knowledge to guide search toward relevant regions without disconnecting the graph.

## The Problem pgvector Has

pgvector's HNSW + post-filter strategy hits two problems:

| Problem | Symptom | pg_acorn Fix |
|---------|---------|--------------|
| **Correlated filter, moderate pass-rate (10–40%)** | pgvector iterative-scan recall@10 falls to ~0.22–0.50 (measured) | Predicate-aware search holds ~0.95–1.0 |
| **Expensive post-filter** | Must scan 1000s of rows to return top-10 | Predicate pushdown bounds traversal to relevant neighbors |
| **Planner falls back to SeqScan** | Index ignored; no top-k guarantee | ACORN ensures top-k under any filter |

## Index Types and Configuration

| Index Type | Storage | Predicate | Use Case |
|------------|---------|-----------|----------|
| `USING hnsw (embedding)` + `pg_acorn` installed | Unchanged | Inferred from WHERE clause | Zero-migration; any integer/text filter |
| `USING acorn_hnsw (embedding, filter_col)` | M×gamma neighbors | Required column | High-selectivity (1–40%); correlated data |
| `USING acorn_hnsw (embedding, filter_col) WITH (acorn_payload_edges=true)` | M×gamma + same-value links | Required column | 1–5% selectivity; vector co-location desired |
| `USING acorn_hnsw (embedding, filter_col) WITH (acorn_inline_vectors=true)` | M×gamma + quantized vectors inline | Required column | Sub-ms latency; 50M+ rows; ~16–20× index size |

## When to Use pg_acorn

| Scenario | Recommendation | Notes |
|----------|---|---|
| Filter selectivity <1%, any index type | pgvector HNSW | Post-filter cost is negligible |
| Filter selectivity 1–40%, correlated vectors | **acorn_hnsw + predicate** | Recall ceiling eliminated; 2–5× faster than post-filter |
| Filter selectivity 10–40%, uncorrelated | pgvector HNSW + post-filter | pg_acorn gains small; pgvector is simpler |
| Filter selectivity >50% | pgvector HNSW | Post-filter is cheapest path |
| Sub-millisecond latency required | **acorn_hnsw + payload_edges + inline_vectors** | Trade ~16–20× index size for 10× latency; measure first |
| 50M+ rows, memory-constrained | pgvector IVFFLAT | Use compression; pg_acorn not optimized for extreme scale |

## Tier 1 — Zero Migration

Install and existing indexes gain ACORN-1 filtered search automatically:

```sql
CREATE EXTENSION pg_acorn;

-- existing index unchanged; planner adds ACORN filtering automatically
CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);

SELECT * FROM items WHERE category = 'shoes'
ORDER BY embedding <=> $1 LIMIT 10;
```

The planner detects the WHERE clause and inserts an ACORN scan path. No index rebuild, no SQL changes.

Supported pgvector versions: 0.8.x+

## Tier 2 — Full ACORN-Gamma

Dedicated `acorn_hnsw` access method with predicate-aware cost estimation and optional optimizations:

```sql
CREATE INDEX ON items USING acorn_hnsw (embedding vector_cosine_ops, category int4_acorn_ops)
  WITH (m = 16, ef_construction = 64, acorn_gamma = 2);
```

**Parameters:**

- `acorn_gamma` (int, 1–4, default 1): Store M×gamma neighbors at build time. Higher gamma improves recall but increases index size and build time.
  - `1`: ACORN-1, search-time neighbor expansion, no build overhead
  - `2+`: ACORN-gamma, M×gamma neighbors in index, higher recall on difficult queries

- `acorn_payload_edges` (bool, default false): Split layer-0 neighbor slots — half global-nearest, half intra-partition edges. Makes same-category subgraph navigable on 1–5% filters without raising gamma.

- `acorn_inline_vectors` (bool, default false): Store SQ8-quantized vectors + TID inline in neighbor slots. Sub-ms latency on large indexes; trades ~16–20× index size for 10× faster scan.

- `pg_acorn.member_first` (GUC, default off): Prefer filter-passing candidates during search. Companion to `acorn_payload_edges`.

Example: correlated, high-selectivity data:

```sql
CREATE INDEX ON items USING acorn_hnsw (embedding, category)
  WITH (m=16, ef_construction=128, acorn_gamma=2, acorn_payload_edges=true);
SET pg_acorn.member_first = on;

-- Result: recall 0.98 @ ef_search=100 (vs 0.94 @ ef_search=400 without payload_edges)
SELECT * FROM items WHERE category = 'electronics'
ORDER BY embedding <=> $1 LIMIT 10;
```

**Driver note:** Bind integer filter constants carefully. The operator family includes cross-type (int2/int4/int8) comparisons, so quals like `category < $1::smallint` (psycopg's default) still push down as index conditions.

## Build and Test

```bash
# Build extension
make
make install

# Run regression tests
make installcheck          # SQL + golden file tests
make isolation-installcheck # concurrent session isolation tests
make unit                  # C unit tests

# Enable in PostgreSQL
psql -c "CREATE EXTENSION pg_acorn;"
```

Requirements: PostgreSQL 16+, pgvector 0.8.x+

## Benchmarks

Reproducible harnesses in [bench/](bench/) compare pg_acorn against pgvector and Qdrant 1.16 on a correlated-filter fixture.

- 3-way (pgvector / Qdrant / acorn): `bench/bench3way_pg.py`, `bench/bench3way_qdrant.py` → [REPORT_3way.md](bench/REPORT_3way.md)
- Scaling (100K / 1M / 10M): `bench/scalebench.py` → [REPORT_scale.md](bench/REPORT_scale.md)
- Overhead decomposition: `bench/overhead_ledger.py` → [OVERHEAD_LEDGER.md](bench/OVERHEAD_LEDGER.md)

Headline (recall@10, correlated filter): acorn holds ~0.97–1.0 where pgvector's iterative scan falls to ~0.22–0.50; latency vs Qdrant is competitive at low pass-rate and INDICATIVE/unresolved at high. The single source of truth — with an explicit "what is solid vs indicative" breakdown — is [bench/COMPETITIVE_VERDICT.md](bench/COMPETITIVE_VERDICT.md).

## Documentation

- [Architecture](docs/architecture.md) — Tier 1 / Tier 2 design, graph layout, scan strategy
- [Competitive verdict](bench/COMPETITIVE_VERDICT.md) — acorn vs pgvector vs Qdrant (single source of truth)
- [Project log](docs/project-log.md) — experiment ledger: what was tried, results, what's open
- [Roadmap](docs/development-roadmap.md) — grand plan, tracks, and the Stabilization gate for 1.0
- [Build-perf notes](docs/build-perf-backlog.md) · [M-ACORN findings](docs/macorn-penalty-findings.md) · [Filtered-HNSW landscape](docs/filterable-hnsw-landscape.md)

## Limitations

- **Single predicate column only** — not arbitrary WHERE expressions
- **Categorical columns best** — <1000 distinct values for efficient encoding
- **No in-place UPDATE** — modified rows trigger index rebuild (delegated to pgvector logic)
- **Tier-2 mode (gamma>1) costs**: 10–20% longer build, higher memory, ~2–3× higher latency for 10% recall gains
- **Not suitable for >50% selectivity** — post-filter is cheaper

## Related Work

- **pgvector**: https://github.com/pgvector/pgvector
- **ACORN paper**: https://arxiv.org/abs/2403.04871
- **HNSW paper**: https://arxiv.org/abs/1603.09320
- **Qdrant filterable HNSW**: https://qdrant.tech/blog/qdrant-1.16.x/
- **Elastic filtered HNSW**: https://www.elastic.co/search-labs/blog/filtered-hnsw-knn-search

---

**License:** Same as pgvector. See [LICENSE](LICENSE).

## Contributing

Issues, benchmark results on your data, and pull requests welcome. Before submitting:

```bash
make test
make test_speed  # benchmark sweep
```

Ensure your changes don't regress recall or QPS on test fixtures.
