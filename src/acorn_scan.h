#ifndef ACORN_SCAN_H
#define ACORN_SCAN_H

#include "postgres.h"
#include "nodes/execnodes.h"
#include "utils/relcache.h"

/*
 * ACORN-1 predicate subgraph traversal.
 *
 * Filter-failing candidates are excluded from the result set W but kept in
 * the traversal queue C, preserving graph connectivity.  This guarantees
 * top-k at any selectivity — the core ACORN invariant.
 *
 * Used by both Tier 1 (CustomScan executor) and Tier 2 (index AM scan).
 */

typedef struct AcornScanState
{
	int			ef_search;		/* candidate list size */
	int			k;				/* requested results */
	ExprState  *predicate;		/* NULL = unfiltered */
	ExprContext *econtext;		/* for predicate evaluation */
} AcornScanState;

/*
 * Execute ACORN-1 traversal starting from entry_element.
 * Returns up to k element ids in result_ids (caller-allocated, size k).
 * Returns actual count (may be < k if graph has fewer matching nodes).
 */
int acorn_scan_execute(AcornScanState *state,
					   Relation index,
					   Datum query_vec,
					   int64 *result_ids);

#endif /* ACORN_SCAN_H */
