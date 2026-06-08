/*
 * acorn_am.c — acorn_hnsw index Access Method handler (Tier 2)
 *
 * Registers the full IndexAmRoutine.  Build/insert live in acorn_build.c;
 * cost estimation in acorn_cost.c.  The scan uses the resumable streaming
 * traversal (acorn_stream_*): a pure index AM does not see the WHERE filter, so
 * it emits heap TIDs in approximate nearest-first order and the executor
 * post-filters on the Index Scan, pulling more as needed.
 */

#include "postgres.h"

#include <float.h>

#include "access/amapi.h"
#include "access/genam.h"
#include "access/relscan.h"
#include "access/reloptions.h"
#include "commands/vacuum.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "storage/bufmgr.h"
#include "utils/hsearch.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"

#include "pg_acorn.h"
#include "acorn_am.h"
#include "acorn_cost.h"
#include "acorn_scan.h"

PG_FUNCTION_INFO_V1(acorn_hnsw_handler);

static relopt_kind acorn_relopt_kind;

/* -----------------------------------------------------------------------
 * Reloptions
 * ----------------------------------------------------------------------- */

void
acorn_am_init(void)
{
	acorn_relopt_kind = add_reloption_kind();
	add_int_reloption(acorn_relopt_kind, "m",
					  "Max number of connections per node",
					  ACORN_DEFAULT_M, ACORN_MIN_M, ACORN_MAX_M,
					  AccessExclusiveLock);
	add_int_reloption(acorn_relopt_kind, "ef_construction",
					  "Size of the dynamic candidate list for construction",
					  ACORN_DEFAULT_EF_CONSTRUCTION,
					  ACORN_MIN_EF_CONSTRUCTION, ACORN_MAX_EF_CONSTRUCTION,
					  AccessExclusiveLock);
	add_int_reloption(acorn_relopt_kind, "acorn_gamma",
					  "ACORN gamma: store m*gamma neighbors per node",
					  ACORN_DEFAULT_GAMMA, ACORN_MIN_GAMMA, ACORN_MAX_GAMMA,
					  AccessExclusiveLock);
}

static bytea *
acorn_options(Datum reloptions, bool validate)
{
	static const relopt_parse_elt tab[] = {
		{"m", RELOPT_TYPE_INT, offsetof(AcornOptions, m)},
		{"ef_construction", RELOPT_TYPE_INT, offsetof(AcornOptions, efConstruction)},
		{"acorn_gamma", RELOPT_TYPE_INT, offsetof(AcornOptions, gamma)},
	};

	return (bytea *) build_reloptions(reloptions, validate,
									  acorn_relopt_kind,
									  sizeof(AcornOptions),
									  tab, lengthof(tab));
}

static bool
acorn_validate(Oid opclassoid)
{
	return true;
}

/* -----------------------------------------------------------------------
 * Scan
 * ----------------------------------------------------------------------- */

/*
 * Hash-table entry deduplicating heap TIDs returned by the batch round against
 * those later emitted by the streaming continuation.  The dummy byte satisfies
 * dynahash's requirement that entrysize > keysize.
 */
typedef struct AcornSeenEntry
{
	ItemPointerData heaptid;
	char			dummy;
} AcornSeenEntry;

/*
 * Tier 2 hybrid scan.  Phase 1: a proven ef=ACORN_DEFAULT_EF_SEARCH beam batch
 * (acorn_scan_execute) — cheap, and at low selectivity with the filter matches
 * concentrated near the query it alone satisfies the executor's LIMIT.  Phase 2
 * (only if the batch is exhausted while the executor still pulls): a resumable
 * streaming frontier (acorn_stream_*) that continues nearest-first WITHOUT
 * re-running the traversal from the entry point, deduped against the batch via
 * `seen`.  This avoids both the old ef-doubling re-traversal blowup (phase 2
 * never restarts) and pure-streaming over-expansion on concentrated matches
 * (phase 1 handles those).
 */
typedef struct AcornScanOpaqueData
{
	bool			 first;
	Datum			 query;			/* detoasted query vector (lives in tmpCtx) */

	/* Phase 1: batch beam search */
	ItemPointerData *batch;			/* top-ef TIDs (in tmpCtx) */
	int				 batch_count;
	int				 batch_pos;

	/* Phase 2: streaming continuation (lazily started) */
	AcornStreamScan *stream;		/* persistent frontier (lives in tmpCtx) */
	HTAB			*seen;			/* TIDs already returned (in tmpCtx) */

	MemoryContext	 tmpCtx;
} AcornScanOpaqueData;
typedef AcornScanOpaqueData *AcornScanOpaque;

static IndexScanDesc
acorn_beginscan(Relation index, int nkeys, int norderbys)
{
	IndexScanDesc	scan = RelationGetIndexScan(index, nkeys, norderbys);
	AcornScanOpaque so   = palloc0(sizeof(AcornScanOpaqueData));

	so->first       = true;
	so->query       = (Datum) 0;
	so->batch       = NULL;
	so->batch_count = 0;
	so->batch_pos   = 0;
	so->stream      = NULL;
	so->seen        = NULL;
	so->tmpCtx      = AllocSetContextCreate(CurrentMemoryContext,
											"acorn scan", ALLOCSET_DEFAULT_SIZES);

	scan->opaque = so;
	return scan;
}

static void
acorn_rescan(IndexScanDesc scan, ScanKey keys, int nkeys,
			 ScanKey orderbys, int norderbys)
{
	AcornScanOpaque so = (AcornScanOpaque) scan->opaque;

	so->first       = true;
	so->query       = (Datum) 0;
	so->batch       = NULL;
	so->batch_count = 0;
	so->batch_pos   = 0;
	so->stream      = NULL;
	so->seen        = NULL;
	MemoryContextReset(so->tmpCtx);

	if (keys && scan->numberOfKeys > 0)
		memmove(scan->keyData, keys, scan->numberOfKeys * sizeof(ScanKeyData));
	if (orderbys && scan->numberOfOrderBys > 0)
		memmove(scan->orderByData, orderbys,
				scan->numberOfOrderBys * sizeof(ScanKeyData));
}

/* Emit one heap TID to the executor and report success. */
static inline bool
acorn_emit(IndexScanDesc scan, ItemPointerData tid)
{
	scan->xs_heaptid        = tid;
	scan->xs_recheck        = false;
	scan->xs_recheckorderby = false;
	return true;
}

/*
 * acorn_gettuple — hybrid two-phase scan.
 *
 * Phase 1 (first call): one ef=ACORN_DEFAULT_EF_SEARCH beam batch via
 * acorn_scan_execute().  Cheap and accurate; when the filter matches are
 * concentrated near the query (low selectivity, correlated data) this batch
 * alone satisfies the executor's LIMIT, so phase 2 never runs.
 *
 * Phase 2 (only if the executor exhausts the batch and keeps pulling): a
 * resumable streaming frontier (acorn_stream_*) that continues nearest-first
 * WITHOUT restarting the traversal, deduplicated against the batch via `seen`.
 * Stops when the graph is exhausted.
 *
 * The executor post-filters on the WHERE predicate, so this AM evaluates none.
 */
static bool
acorn_gettuple(IndexScanDesc scan, ScanDirection dir)
{
	AcornScanOpaque so = (AcornScanOpaque) scan->opaque;
	MemoryContext	oldCtx;
	ItemPointerData tid;

	Assert(ScanDirectionIsForward(dir));

	if (scan->orderByData == NULL)
		elog(ERROR, "cannot scan acorn_hnsw index without ORDER BY");
	if (!IsMVCCSnapshot(scan->xs_snapshot))
		elog(ERROR, "non-MVCC snapshots are not supported with acorn_hnsw");

	/* NULL order-by value yields no rows */
	if (scan->orderByData->sk_flags & SK_ISNULL)
	{
		so->first = false;
		return false;
	}

	/* Phase 1 setup: detoast query and run the ef-beam batch once */
	if (so->first)
	{
		AcornScanState st;

		oldCtx = MemoryContextSwitchTo(so->tmpCtx);

		so->query = PointerGetDatum(
			PG_DETOAST_DATUM(scan->orderByData->sk_argument));

		so->batch = palloc(sizeof(ItemPointerData) * ACORN_DEFAULT_EF_SEARCH);
		st.ef_search = ACORN_DEFAULT_EF_SEARCH;
		st.k         = ACORN_DEFAULT_EF_SEARCH;
		st.predicate = NULL;
		st.econtext  = NULL;
		so->batch_count = acorn_scan_execute(&st, scan->indexRelation, NULL,
											 so->query, scan->xs_snapshot,
											 so->batch);
		so->batch_pos = 0;
		so->first = false;

		MemoryContextSwitchTo(oldCtx);
	}

	/* Phase 1: drain the batch (already sorted nearest-first) */
	if (so->batch_pos < so->batch_count)
		return acorn_emit(scan, so->batch[so->batch_pos++]);

	/* Phase 2: streaming continuation — start it lazily, seeding `seen` */
	oldCtx = MemoryContextSwitchTo(so->tmpCtx);

	if (so->stream == NULL)
	{
		HASHCTL hctl;

		memset(&hctl, 0, sizeof(hctl));
		hctl.keysize   = sizeof(ItemPointerData);
		hctl.entrysize = sizeof(AcornSeenEntry);
		hctl.hcxt      = so->tmpCtx;
		so->seen = hash_create("acorn_seen", 64, &hctl,
							   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);

		for (int i = 0; i < so->batch_count; i++)
			hash_search(so->seen, &so->batch[i], HASH_ENTER, NULL);

		so->stream = acorn_stream_begin(scan->indexRelation, so->query,
										scan->xs_snapshot, so->tmpCtx);
	}

	while (acorn_stream_next(so->stream, &tid))
	{
		bool found;

		hash_search(so->seen, &tid, HASH_ENTER, &found);
		if (found)
			continue;				/* already returned by the batch or earlier */

		MemoryContextSwitchTo(oldCtx);
		return acorn_emit(scan, tid);
	}

	MemoryContextSwitchTo(oldCtx);
	return false;
}

static void
acorn_endscan(IndexScanDesc scan)
{
	AcornScanOpaque so = (AcornScanOpaque) scan->opaque;

	MemoryContextDelete(so->tmpCtx);
	pfree(so);
	scan->opaque = NULL;
}

/* -----------------------------------------------------------------------
 * Vacuum (minimal — no dead-tuple reclamation; sufficient for fresh indexes)
 * ----------------------------------------------------------------------- */

static IndexBulkDeleteResult *
acorn_bulkdelete(IndexVacuumInfo *info, IndexBulkDeleteResult *stats,
				 IndexBulkDeleteCallback callback, void *callback_state)
{
	if (stats == NULL)
		stats = (IndexBulkDeleteResult *) palloc0(sizeof(IndexBulkDeleteResult));
	return stats;
}

static IndexBulkDeleteResult *
acorn_vacuumcleanup(IndexVacuumInfo *info, IndexBulkDeleteResult *stats)
{
	if (info->analyze_only)
		return stats;
	if (stats == NULL)
		stats = (IndexBulkDeleteResult *) palloc0(sizeof(IndexBulkDeleteResult));
	stats->num_pages = RelationGetNumberOfBlocks(info->index);
	return stats;
}

/* -----------------------------------------------------------------------
 * Handler
 * ----------------------------------------------------------------------- */

Datum
acorn_hnsw_handler(PG_FUNCTION_ARGS)
{
	IndexAmRoutine *amroutine = makeNode(IndexAmRoutine);

	amroutine->amstrategies = 0;
	amroutine->amsupport = 3;
	amroutine->amoptsprocnum = 0;
	amroutine->amcanorder = false;
	amroutine->amcanorderbyop = true;
	amroutine->amcanbackward = false;
	amroutine->amcanunique = false;
	amroutine->amcanmulticol = false;
	amroutine->amoptionalkey = true;
	amroutine->amsearcharray = false;
	amroutine->amsearchnulls = false;
	amroutine->amstorage = false;
	amroutine->amclusterable = false;
	amroutine->ampredlocks = false;
	amroutine->amcanparallel = false;
#if PG_VERSION_NUM >= 170000
	amroutine->amcanbuildparallel = false;
#endif
	amroutine->amcaninclude = false;
	amroutine->amusemaintenanceworkmem = false;
#if PG_VERSION_NUM >= 160000
	amroutine->amsummarizing = false;
#endif
	amroutine->amparallelvacuumoptions = VACUUM_OPTION_PARALLEL_BULKDEL;
	amroutine->amkeytype = InvalidOid;

	/* Interface functions */
	amroutine->ambuild = acorn_build;
	amroutine->ambuildempty = acorn_buildempty;
	amroutine->aminsert = acorn_insert;
#if PG_VERSION_NUM >= 170000
	amroutine->aminsertcleanup = NULL;
#endif
	amroutine->ambulkdelete = acorn_bulkdelete;
	amroutine->amvacuumcleanup = acorn_vacuumcleanup;
	amroutine->amcanreturn = NULL;
	amroutine->amcostestimate = acorn_hnsw_costestimate;
	amroutine->amoptions = acorn_options;
	amroutine->amproperty = NULL;
	amroutine->ambuildphasename = NULL;
	amroutine->amvalidate = acorn_validate;
#if PG_VERSION_NUM >= 140000
	amroutine->amadjustmembers = NULL;
#endif
	amroutine->ambeginscan = acorn_beginscan;
	amroutine->amrescan = acorn_rescan;
	amroutine->amgettuple = acorn_gettuple;
	amroutine->amgetbitmap = NULL;
	amroutine->amendscan = acorn_endscan;
	amroutine->ammarkpos = NULL;
	amroutine->amrestrpos = NULL;

	amroutine->amestimateparallelscan = NULL;
	amroutine->aminitparallelscan = NULL;
	amroutine->amparallelrescan = NULL;

	PG_RETURN_POINTER(amroutine);
}
