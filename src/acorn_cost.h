#ifndef ACORN_COST_H
#define ACORN_COST_H

#include "postgres.h"
#include "nodes/pathnodes.h"

/*
 * amcostestimate for acorn_hnsw.
 *
 * Uses clauselist_selectivity() to obtain the fraction of rows matching the
 * filter predicates, then scales the index scan cost accordingly.  A highly
 * selective filter (small fraction) results in lower cost than a full HNSW
 * scan, so the planner automatically prefers acorn_hnsw over seq scan.
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
