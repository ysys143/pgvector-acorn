# Architecture

## Why Two Tiers

A single approach cannot satisfy both zero-migration UX and full ACORN-gamma capability:

| Concern | Tier 1 (hook) | Tier 2 (acorn_hnsw AM) |
|---------|--------------|------------------------|
| Migration cost | none | index rebuild required |
| ACORN variant | ACORN-1 (search-time only) | ACORN-gamma (M*gamma at build) |
| pgvector dependency | pgvector 0.8.x page layout | none |
| Upstream PR target | no (external hook) | yes (algorithm core) |
| Planner integration | CustomPath via hook | amcostestimate |

Tier 1 is the adoption path. Tier 2 is the upstream PR target.

---

## Tier 1: Hook + CustomScan

### Detection

`set_rel_pathlist_hook` fires for every relation during path generation. pg_acorn checks:

1. Does the relation have an index with `amhandler` registered as `hnsw_handler`?
   (lookup via `pg_am` by name `'hnsw'`)
2. Does the query's `RestrictInfo` list contain a vector distance operator
   (`<->`, `<=>`, `<#>`) in the ORDER BY / `KNNScan` path?
3. Is there at least one additional scalar qual (the WHERE filter)?

If all three hold, pg_acorn adds a `CustomPath` to the planner's path list.

### Cost Estimation

```c
double filter_selectivity = clauselist_selectivity(root, filter_clauses,
                                                   rel->relid, JOIN_INNER, NULL);
/* Fewer matching nodes → more traversal needed → higher CPU cost */
/* But still cheaper than seq scan when selectivity > ~5% */
custom_path->path.total_cost = hnsw_cost / filter_selectivity * ACORN_OVERHEAD;
```

The planner picks the CustomPath when it beats the seq scan cost estimate.

### CustomScan Executor

The executor reads pgvector 0.8.x HNSW page layout directly:
- Page type identification via `HnswPageGetOpaque()->page_type`
- Element tuple reading via `HnswGetElementFromPage()`
- Neighbor tuple reading via `HnswGetNeighborFromPage()`

**pgvector version pinning**: This is a hard dependency on pgvector 0.8.x internal
layout. `pg_acorn.control` declares `requires = 'vector'` and the hook checks
`get_extension_version('vector')` at load time, refusing to activate if the version
is outside the supported range.

### ACORN-1 Traversal (acorn_scan.c — shared with Tier 2)

The core predicate subgraph logic lives in `acorn_scan.c` and is used by both tiers:

```c
for each unvisited neighbor eElement at distance eDistance:
    if eDistance >= furthest_result && !always_add:
        break  /* standard HNSW early termination */

    bool matches = predicate == NULL
                   || ExecQualAndReset(predicate_state, econtext);

    if (matches):
        add_to_W(W, eElement, eDistance)   /* include in results */
    else:
        add_to_C(C, eElement, eDistance)   /* traverse neighbors, skip results */
        /* ACORN key: graph connectivity preserved even when node filtered out */
```

The `else` branch is the only difference from vanilla HNSW. Filtered-out nodes stay
in the candidate queue C so their neighbors remain reachable.

---

## Tier 2: acorn_hnsw Index AM

### Index AM Callbacks

```c
static IndexAmRoutine acorn_hnsw_routine = {
    .amstrategies   = 0,
    .amsupport      = 4,           /* same operator classes as pgvector */
    .amcanorderbyop = true,
    .amoptionalkey  = true,
    .amsearcharray  = false,
    .aminsert       = acorn_insert,
    .ambuild        = acorn_build,
    .ambuildempty   = acorn_buildempty,
    .ambulkdelete   = acorn_bulkdelete,
    .amvacuumcleanup = acorn_vacuumcleanup,
    .amcostestimate = acorn_costestimate,
    .amoptions      = acorn_options,
    .amgettuple     = acorn_gettuple,
    .amgetbitmap    = NULL,
};
```

### Reloptions

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| `m` | 16 | 2–100 | Max neighbors per node |
| `ef_construction` | 64 | 4–1000 | Candidate pool during build |
| `acorn_gamma` | 1 | 1–8 | Neighbor expansion multiplier |

`acorn_gamma = 1` stores M neighbors (ACORN-1, no build overhead).
`acorn_gamma = 2` stores 2M candidates (ACORN-gamma, paper reports 11x build overhead,
significant recall improvement under high-selectivity filters).

### Build: M*gamma Neighbor Storage (acorn_build.c)

At index build time, `SelectNeighbors()` considers `m * acorn_gamma` candidates instead
of `m`. The stored neighbor list still has fixed size `m * acorn_gamma` slots per layer.

### Incremental Insert: Fixed-Slot Retry

pgvector has a known defect (marked `TODO`) where bidirectional edge updates silently
skip connections when neighbor tuple slots are full. acorn_build.c fixes this:

```c
static void
AcornUpdateNeighbor(Relation index, HnswElement element,
                    HnswElement neighbor, int layer)
{
    /* Attempt to add element to neighbor's list */
    if (neighbor->neighbors[layer].length < neighbor->neighbors[layer].capacity)
    {
        AppendNeighbor(&neighbor->neighbors[layer], element);
        return;
    }

    /* Slots full: replace the furthest neighbor if element is closer */
    HnswElement furthest = FindFurthestNeighbor(&neighbor->neighbors[layer]);
    if (element->distance < furthest->distance)
        ReplaceNeighbor(&neighbor->neighbors[layer], furthest, element);
    /* Otherwise: element is genuinely not a good neighbor, skip is correct */
}
```

This is a correctness fix, not a performance optimization. Without it, incremental
inserts produce an incomplete graph that degrades recall silently over time.

### Cost Estimation (acorn_cost.c)

`amcostestimate` is the mechanism by which the planner decides to use acorn_hnsw
over a seq scan. The key is accounting for filter selectivity:

```c
void
acorn_costestimate(PlannerInfo *root, IndexPath *path, ...)
{
    double filter_sel = clauselist_selectivity(root, path->indexquals, ...);
    double matching   = index->tuples * filter_sel;

    /* ACORN traversal cost scales sub-linearly with selectivity */
    *indexTotalCost = log2(matching + 1) * cpu_operator_cost * ef_search;

    /* seq scan cost for comparison: index wins when filter_sel > ~0.02 */
    *indexSelectivity = filter_sel;
}
```

Without this, the planner uses the default generic cost estimate and prefers seq scan.

---

## Benchmark Design

See `bench/` for the full harness. Four scenarios are designed to isolate specific
failure modes:

| Scenario | What it exposes |
|----------|----------------|
| A — selectivity sweep | ACORN stable across selectivity; pgvector recall collapses |
| B — post-filter recall | CTE workaround cannot guarantee top-k; quantifies oversample cost |
| C — incremental recall | Graph quality degrades without fixed-slot retry |
| D — correlation | ACORN handles adversarial filter-query misalignment |

Each scenario is self-contained: independent fixture, independent DB schema,
no shared state between runs.

---

## File Map

```
src/pg_acorn.c      _PG_init(): register hook + AM
src/acorn_hook.c    Tier 1: set_rel_pathlist_hook, CustomScan provider
src/acorn_am.c      Tier 2: IndexAmRoutine, operator class bindings
src/acorn_build.c   Tier 2: batch build (M*gamma), incremental insert + retry
src/acorn_scan.c    Shared: predicate subgraph traversal (ACORN-1 core)
src/acorn_cost.c    Tier 2: amcostestimate with filter selectivity
```
