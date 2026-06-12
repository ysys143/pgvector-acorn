#ifndef ACORN_COST_H
#define ACORN_COST_H

#include "postgres.h"
#include "nodes/pathnodes.h"

/*
 * amcostestimate for acorn_hnsw.
 *
 * Uses clauselist_selectivity() to obtain the fraction of rows matching the
 * filter predicates.  In-filter cost ~ ef_search / selectivity: a highly
 * selective filter makes the acorn scan MORE expensive (it must expand through
 * many filter-failing nodes to collect enough passing results), so the planner
 * prefers a bitmap prefilter at high selectivity and the acorn_hnsw scan only
 * in the looser mid/low-selectivity range.
 */
void acorn_hnsw_costestimate(PlannerInfo *root,
							 IndexPath *path,
							 double loop_count,
							 Cost *indexStartupCost,
							 Cost *indexTotalCost,
							 Selectivity *indexSelectivity,
							 double *indexCorrelation,
							 double *indexPages);

#endif /* ACORN_COST_H */
