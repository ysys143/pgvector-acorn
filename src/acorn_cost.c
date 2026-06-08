/*
 * acorn_cost.c — amcostestimate for acorn_hnsw (Tier 2)
 *
 * An ORDER-BY-only index: returns +infinity without an ORDER BY (it only
 * answers nearest-k).  Cost is charged per RETURNED tuple by graph-node
 * expansion (see ACORN_PAGES_PER_TUPLE), because the WHERE predicate is a heap
 * Filter, not an index qual — so a low-selectivity filter that forces many
 * ordered tuples to be emitted is correctly expensive.  This yields the right
 * index-vs-seqscan crossover: seq scan wins on small tables / low selectivity,
 * the index wins at high selectivity on large tables.
 */

#include "postgres.h"

#include <math.h>

#include "access/amapi.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "utils/float.h"
#include "utils/selfuncs.h"
#include "utils/spccache.h"

#include "acorn_am.h"
#include "acorn_cost.h"

/*
 * Random page fetches charged per returned (emitted) tuple: emitting the next
 * nearest neighbour expands one graph node — reading its neighbour list and a
 * handful of neighbour vectors.  This is the dominant ACORN scan cost and is
 * paid per result, so it governs the index-vs-seqscan crossover.
 */
#define ACORN_PAGES_PER_TUPLE	16.0

void
acorn_hnsw_costestimate(PlannerInfo *root,
						IndexPath *path,
						double loop_count,
						Cost *indexStartupCost,
						Cost *indexTotalCost,
						Selectivity *indexSelectivity,
						double *indexCorrelation,
						double *indexPages)
{
	GenericCosts costs;
	double		spc_seq_cost;
	double		spc_rand_cost;
	double		n_tuples;

	/* Never use the index without an ORDER BY (it only answers nearest-k) */
	if (path->indexorderbys == NULL)
	{
		*indexStartupCost = get_float8_infinity();
		*indexTotalCost = get_float8_infinity();
		*indexSelectivity = 0;
		*indexCorrelation = 0;
		*indexPages = 0;
		return;
	}

	MemSet(&costs, 0, sizeof(costs));
	genericcostestimate(root, path, loop_count, &costs);

	get_tablespace_page_costs(path->indexinfo->reltablespace,
							  &spc_rand_cost, &spc_seq_cost);

	n_tuples = costs.numIndexTuples;
	if (n_tuples < 1)
		n_tuples = path->indexinfo->tuples;
	if (n_tuples < 1)
		n_tuples = 1;

	/*
	 * ACORN/HNSW cost model.  Unlike an ordinary index, the dominant cost is
	 * paid *per returned tuple*: emitting the next nearest neighbour expands a
	 * graph node, reading its neighbour list plus several neighbour vectors
	 * (~ACORN_PAGES_PER_TUPLE random page fetches).  Because the WHERE predicate
	 * is a heap Filter (not an index qual), a low-selectivity filter forces the
	 * executor to pull ~k/selectivity ordered tuples before its LIMIT is met, so
	 * this per-tuple price makes the index correctly expensive at low
	 * selectivity / small tables (seq scan wins) and cheap only at high
	 * selectivity on large tables (index wins).  Startup is the greedy descent
	 * to the base-layer entry point (~log N page reads).
	 */
	costs.indexStartupCost = log(Max(n_tuples, 2.0)) * spc_rand_cost;
	costs.indexTotalCost = costs.indexStartupCost
		+ n_tuples * ACORN_PAGES_PER_TUPLE * spc_rand_cost;

	*indexStartupCost = costs.indexStartupCost;
	*indexTotalCost = costs.indexTotalCost;
	*indexSelectivity = costs.indexSelectivity;
	*indexCorrelation = costs.indexCorrelation;
	*indexPages = costs.numIndexPages;
}
