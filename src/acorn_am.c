/*
 * acorn_am.c — acorn_hnsw index Access Method handler (Tier 2)
 *
 * Registers the full IndexAmRoutine.  Build/insert live in acorn_build.c;
 * cost estimation in acorn_cost.c; the scan reuses the shared ACORN traversal
 * acorn_scan_execute() (predicate = NULL — a pure index AM does not see the
 * WHERE filter; the executor post-filters on the Index Scan).
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
 * Maximum ef allowed during iterative expansion.  At 1% selectivity with
 * k=10, ef ≈ 1000 is needed for good recall; 4096 gives comfortable headroom.
 */
#define ACORN_EF_SEARCH_MAX  4096

/*
 * Hash-table entry for the "seen" set that deduplicates heap TIDs across
 * ef-expansion rounds.  The dummy byte is required because HTAB entrysize
 * must be strictly larger than keysize.
 */
typedef struct AcornSeenEntry
{
	ItemPointerData heaptid;
	char			dummy;
} AcornSeenEntry;

typedef struct AcornScanOpaqueData
{
	bool			first;
	bool			done;			/* true when no more expansions are possible */
	Datum			query;			/* detoasted query vector (lives in tmpCtx) */
	int				ef;				/* current search size (doubles on each expansion) */
	ItemPointerData *tids;			/* current search result buffer (in tmpCtx) */
	int				count;
	int				pos;
	HTAB		   *seen;			/* heap TIDs already returned (in tmpCtx) */
	MemoryContext	tmpCtx;
} AcornScanOpaqueData;
typedef AcornScanOpaqueData *AcornScanOpaque;

static IndexScanDesc
acorn_beginscan(Relation index, int nkeys, int norderbys)
{
	IndexScanDesc	scan = RelationGetIndexScan(index, nkeys, norderbys);
	AcornScanOpaque so   = palloc0(sizeof(AcornScanOpaqueData));

	so->first  = true;
	so->done   = false;
	so->ef     = ACORN_DEFAULT_EF_SEARCH;
	so->tmpCtx = AllocSetContextCreate(CurrentMemoryContext,
									   "acorn scan", ALLOCSET_DEFAULT_SIZES);
	so->tids   = NULL;
	so->count  = 0;
	so->pos    = 0;
	so->seen   = NULL;

	scan->opaque = so;
	return scan;
}

static void
acorn_rescan(IndexScanDesc scan, ScanKey keys, int nkeys,
			 ScanKey orderbys, int norderbys)
{
	AcornScanOpaque so = (AcornScanOpaque) scan->opaque;

	so->first  = true;
	so->done   = false;
	so->ef     = ACORN_DEFAULT_EF_SEARCH;
	so->count  = 0;
	so->pos    = 0;
	so->tids   = NULL;
	so->seen   = NULL;
	MemoryContextReset(so->tmpCtx);

	if (keys && scan->numberOfKeys > 0)
		memmove(scan->keyData, keys, scan->numberOfKeys * sizeof(ScanKeyData));
	if (orderbys && scan->numberOfOrderBys > 0)
		memmove(scan->orderByData, orderbys,
				scan->numberOfOrderBys * sizeof(ScanKeyData));
}

/*
 * acorn_gettuple — iterative scan with ef expansion.
 *
 * On the first call, search with ef = ACORN_DEFAULT_EF_SEARCH (40) and
 * buffer the results.  Return heap TIDs one at a time to the executor,
 * which post-filters on the WHERE predicate.  When the buffer is exhausted
 * before the executor stops asking, double ef and search again, skipping TIDs
 * already returned (tracked in so->seen).  Stop when ef >= ACORN_EF_SEARCH_MAX
 * or an expansion produces no new TIDs.
 *
 * This lifts Tier 2 recall at low selectivity: at 1% selectivity with k=10
 * the executor needs ~1000 candidates, requiring ~5 doublings from ef=40.
 */
static bool
acorn_gettuple(IndexScanDesc scan, ScanDirection dir)
{
	AcornScanOpaque so = (AcornScanOpaque) scan->opaque;

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

	/* First call: detoast query, allocate seen set, run initial search */
	if (so->first)
	{
		MemoryContext	oldCtx = MemoryContextSwitchTo(so->tmpCtx);
		AcornScanState	st;
		HASHCTL			hctl;

		so->query = PointerGetDatum(
			PG_DETOAST_DATUM(scan->orderByData->sk_argument));

		memset(&hctl, 0, sizeof(hctl));
		hctl.keysize   = sizeof(ItemPointerData);
		hctl.entrysize = sizeof(AcornSeenEntry);
		so->seen = hash_create("acorn_seen", 64, &hctl,
							   HASH_ELEM | HASH_BLOBS);

		so->tids  = palloc(sizeof(ItemPointerData) * so->ef);
		st.ef_search = so->ef;
		st.k         = so->ef;
		st.predicate = NULL;
		st.econtext  = NULL;
		so->count = acorn_scan_execute(&st, scan->indexRelation, NULL,
									   so->query, scan->xs_snapshot, so->tids);
		so->pos   = 0;
		so->first = false;

		MemoryContextSwitchTo(oldCtx);
	}

	for (;;)
	{
		/* Return the next TID that has not been returned before */
		while (so->pos < so->count)
		{
			ItemPointerData tid = so->tids[so->pos++];
			bool			found;

			hash_search(so->seen, &tid, HASH_FIND, &found);
			if (!found)
			{
				hash_search(so->seen, &tid, HASH_ENTER, &found);
				scan->xs_heaptid        = tid;
				scan->xs_recheck        = false;
				scan->xs_recheckorderby = false;
				return true;
			}
		}

		/* Buffer exhausted: try expanding ef */
		if (so->done || so->ef >= ACORN_EF_SEARCH_MAX)
			return false;

		{
			MemoryContext	oldCtx = MemoryContextSwitchTo(so->tmpCtx);
			AcornScanState	st;
			bool			any_new = false;

			so->ef    = Min(so->ef * 2, ACORN_EF_SEARCH_MAX);
			so->tids  = repalloc(so->tids, sizeof(ItemPointerData) * so->ef);

			st.ef_search = so->ef;
			st.k         = so->ef;
			st.predicate = NULL;
			st.econtext  = NULL;
			so->count = acorn_scan_execute(&st, scan->indexRelation, NULL,
										   so->query, scan->xs_snapshot,
										   so->tids);
			so->pos = 0;

			/* Check if expansion produced at least one TID we haven't seen */
			for (int i = 0; i < so->count; i++)
			{
				bool found;
				hash_search(so->seen, &so->tids[i], HASH_FIND, &found);
				if (!found)
				{
					any_new = true;
					break;
				}
			}

			if (!any_new)
				so->done = true;

			MemoryContextSwitchTo(oldCtx);
		}
	}
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
