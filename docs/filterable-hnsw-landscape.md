# Filtered Vector Search: Landscape Survey

## The Problem

HNSW-based ANN search breaks down when a WHERE clause is added. Two failure modes:

1. **Planner fallback**: The query optimizer abandons the HNSW index entirely and performs a
   sequential scan. pgvector exhibits this today.

2. **Graph disconnection**: Even when the index scan survives, high filter selectivity pushes the
   retained node count below the percolation threshold (`p_c = 1/⟨k⟩`). The greedy traversal
   gets stranded in a disconnected subgraph and converges on wrong neighbors.

CTE post-filtering avoids the index problem but cannot guarantee top-k (the required oversample
multiplier is unknown) and pays the full ANN cost for a result set it then discards.

---

## Taxonomy of Approaches

### Post-filtering

```
ANN on full graph → filter results
```

- pgvector's current fallback behavior (seq scan)
- Does not guarantee top-k
- Recall degrades proportionally to filter selectivity

### Prefiltering (strict)

```
scalar filter → candidate ID set → ANN restricted to that set
```

- Restricts the search space upfront using a scalar index (inverted, sorted, trie)
- Works well when many rows pass the filter (low selectivity)
- Breaks when the filtered subset is too small: HNSW graph connectivity collapses below the
  percolation threshold, same failure mode as the original problem but shifted into the subgraph

### Inline filtering (naive)

```
HNSW traversal → skip node if filter fails (from results AND from traversal)
```

- Filter is evaluated per node during traversal
- Nodes that fail the filter are excluded from both results and further neighbor exploration
- Graph connectivity is not preserved: high selectivity causes the same disconnection problem

### ACORN — filterable HNSW

```
HNSW traversal → skip node from results if filter fails, but keep in traversal candidate queue
```

- Filter-failing nodes are excluded from the result set W
- They remain in the exploration queue C, so their neighbors are still visited
- Graph connectivity is preserved regardless of filter selectivity
- top-k is guaranteed by construction

ACORN has two variants:

| Variant | Build-time cost | Search-time expansion |
|---------|----------------|----------------------|
| ACORN-1 | none (M neighbors, same as vanilla HNSW) | explores neighbors-of-neighbors at query time |
| ACORN-gamma | 11x overhead (stores M*gamma candidates) | richer neighbor pool at build time |

Reference: _ACORN: Performant Predicate-Agnostic Hybrid Search_, arxiv 2403.04871

---

## Why ACORN is the Bar

Every other approach has a selectivity sweet spot where it works and a range where it fails:

| Approach | Works well when | Fails when |
|----------|----------------|------------|
| Post-filtering | filter passes most rows | high selectivity → top-k gap |
| Prefiltering | filter passes few rows | filter passes many rows → full graph anyway; or too few → disconnected subgraph |
| Inline (naive) | filter passes most rows | high selectivity → graph disconnection |
| **ACORN** | **any selectivity** | none — graph connectivity preserved by design |

ACORN is selectivity-agnostic. It does not require the caller to tune oversample ratios or know
filter distribution in advance. This is the property that makes it the correct baseline for
production filtered vector search.

---

## System Survey

### pgvector

- **Approach**: post-filtering (seq scan fallback)
- **Mechanism**: planner abandons HNSW index when WHERE clause is present; `hnswcostestimate()`
  does not account for filter selectivity
- **Gap**: no filterable HNSW of any kind

### Milvus

- **Approach**: bitset-based prefiltering
- **Mechanism**: scalar index (inverted / sorted / trie) generates a dense bitset; HNSW traversal
  checks `bitset[node_id]` per candidate and skips zero-bits
- **Type**: naive inline via bitset — filtered nodes are excluded from both results and traversal
- **Limitation**: bitset check is O(1) per node but graph connectivity is not preserved under high
  selectivity

### Elastic (Lucene)

- **Approach**: ACORN-1
- **Mechanism**: neighbors-of-neighbors exploration when >10% of immediate neighbors are filtered
  out; exploration bounded at `M * M` candidates (e.g., 32 x 32 = 1024); early stop once
  `neighborCount * 1/(1 - filterRatio)` vectors are scored
- **Performance**: up to 5x faster than naive approaches on semi-restrictive filters (>= 40%
  filtered), with minimal recall drop
- **Reference**: https://www.elastic.co/search-labs/blog/filtered-hnsw-knn-search

### Oracle Database 26

- **Approach**: planner-selected prefilter or in-filter; no ACORN
- **Prefilter mechanism**: scalar predicate applied to base table → rowid list materialized → hash
  join or nested loop with auxiliary mapping table (`VECTOR$<idx>$HNSW_ROWID_VID_MAP`) → HNSW
  traversal over mapped vector IDs only. NOT bitmap-based; rowid-list + JOIN.
- **In-filter mechanism**: HNSW traversal with per-candidate `TABLE ACCESS BY USER ROWID` heap
  fetch to evaluate predicate. Naive inline skip — filter-failing nodes are excluded from traversal
  as well as results. Recommended when many rows pass the filter (low selectivity), which is the
  inverse of ACORN's strength.
- **Optimizer selection**: `clauselist_selectivity` estimate determines which plan wins
- **Reference**: https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/optimizer-plans-hnsw-vector-indexes.html

### Weaviate

- **Approach**: ACORN with adaptive switching and smart entry points
- **Mechanism**: two-hop neighborhood expansion (neighbors-of-neighbors); adaptive switch between
  standard HNSW and ACORN based on filter density; filter-matching nodes used as entry points for
  low-correlation filter scenarios; no reindexing required for existing HNSW indexes
- **Performance**: up to 10x improvement in adversarial scenarios (low filter-query correlation)
- **Reference**: https://weaviate.io/blog/speed-up-filtered-vector-search

### MongoDB Atlas Vector Search

- **Approach**: prefiltering
- **Mechanism**: internal implementation not publicly documented; fields are indexed separately and
  an MQL match expression restricts the search space before ANN; likely inverted index -> candidate
  ID set, similar to Milvus's bitset approach but not confirmed
- **User impact**: pgvector users have migrated to MongoDB specifically for this capability, citing
  reduced ANN recall under filters
- **Reference**: https://dev.to/mongodb/no-pre-filtering-in-pgvector-means-reduced-ann-recall-1aa1

### Qdrant 1.16+

- **Approach**: ACORN (first-class, native implementation)
- **Mechanism**: predicate subgraph traversal as described in the paper; combined with inline
  storage (quantized vectors embedded directly in HNSW graph nodes for disk-efficient paged reads)
- **Performance**: 97.20% recall vs 53.34% baseline under multi-predicate low-selectivity filters;
  10x throughput improvement on memory-constrained systems via inline storage
- **Reference**: https://qdrant.tech/blog/qdrant-1.16.x/

---

## Positioning of pg_acorn

pgvector is the only widely-deployed PostgreSQL vector extension with no filterable HNSW of any
kind. Every other production vector database — Qdrant, Elastic/Lucene, Weaviate — has shipped
ACORN. MongoDB and Milvus have prefiltering. Oracle has a planner-selected hybrid.

pg_acorn fills this gap within the PostgreSQL ecosystem using a two-tier architecture:

| Tier | Index | Filter mechanism | Migration cost |
|------|-------|-----------------|----------------|
| 1 (hook) | existing `USING hnsw` | ACORN-1 via `set_rel_pathlist_hook` + CustomScan | none |
| 2 (native AM) | `USING acorn_hnsw` | ACORN-gamma via dedicated index AM + correct `amcostestimate` | index rebuild |

Benchmark targets: pgvector vanilla (baseline), pg_acorn Tier 1, pg_acorn Tier 2, Qdrant 1.16+.
Filter selectivity sweep: 1% / 5% / 10% / 40% / 80%.
Metrics: recall@10, QPS, p99 latency.
