/*
 * acorn_cost.c — amcostestimate for acorn_hnsw (Tier 2)
 *
 * Two modes:
 *
 * Pure ORDER BY (no index quals): cost is per RETURNED tuple; the WHERE
 * predicate is a heap Filter, so low selectivity forces many emitted tuples.
 * Seq scan wins on small tables / low selectivity; index wins at high
 * selectivity on large tables.
 *
 * In-filter (index quals present): ACORN-gamma evaluates the scalar predicate
 * inside the graph traversal.  Filter-failing nodes stay in the candidate set
 * for connectivity; only passing nodes go to the result set.  Expansion count
 * is bounded by ef_search / selectivity (not N * selectivity), so cost grows
 * mildly as selectivity drops rather than linearly.  No heap fetch needed.
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

/* Pure ORDER BY: pages charged per returned (emitted) tuple */
#define ACORN_PAGES_PER_TUPLE		16.0

/* In-filter: pages per expanded graph node (element page + neighbor page) */
#define ACORN_T2_PAGES_PER_NODE		2.0

/* In-filter: typical ef_search value used to bound expansion count.
 * NOTE: deliberately a constant, not the live pg_acorn.ef_search GUC. Using the
 * real ef would make the planner abandon the acorn scan for a bitmap prefilter
 * at high ef even in the mid-selectivity band where acorn is empirically 6-8x
 * faster (n=30K correlated: g4 0.955@203qps vs bitmap 1.0@23qps at 40%). The
 * cost model has no recall signal, so a naive ef-aware cost mis-trades speed
 * for an unneeded exact path. Revisit as a deliberate recall-aware calibration. */
#define ACORN_T2_EF_SEARCH_EST		40.0

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

	if (path->indexclauses != NIL)
	{
		/*
		 * In-filter mode: scalar qual evaluated inside graph traversal.
		 * Expansion count is bounded by ef_search / selectivity (not
		 * N * selectivity) because filter-failing nodes are kept in the
		 * candidate set for connectivity and do not become results.
		 */
		Selectivity sel = costs.indexSelectivity;
		double		N_total = Max((double) path->indexinfo->tuples, 1.0);
		double		n_expand;

		if (sel <= 0.0)
			sel = 0.001;
		n_expand = Min(ACORN_T2_EF_SEARCH_EST / sel, N_total);

		costs.indexStartupCost = log(Max(N_total, 2.0)) * spc_rand_cost;
		costs.indexTotalCost = costs.indexStartupCost
			+ n_expand * ACORN_T2_PAGES_PER_NODE * spc_rand_cost;
	}
	else
	{
		/*
		 * Pure ORDER BY: no scalar qual; WHERE predicate is a heap Filter.
		 * Cost scales with returned tuples — low-selectivity filters force
		 * many emitted tuples before the LIMIT is satisfied.
		 */
		costs.indexStartupCost = log(Max(n_tuples, 2.0)) * spc_rand_cost;
		costs.indexTotalCost = costs.indexStartupCost
			+ n_tuples * ACORN_PAGES_PER_TUPLE * spc_rand_cost;
	}

	*indexStartupCost = costs.indexStartupCost;
	*indexTotalCost = costs.indexTotalCost;
	*indexSelectivity = costs.indexSelectivity;
	*indexCorrelation = costs.indexCorrelation;
	*indexPages = costs.numIndexPages;
}
