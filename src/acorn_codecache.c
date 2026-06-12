/*
 * acorn_codecache.c — per-index shared-memory SQ8 code cache (M1: read path)
 *
 * Registry: one named DSM segment ("pg_acorn_cc", PG17 dsm_registry) holds a
 * directory of per-index slots keyed by (dboid, relfilenumber).  Each slot
 * owns a DSA area containing a flat per-block directory: a dsa_pointer array
 * indexed by block number, each pointing to a fixed-stride array of entries
 * indexed by (offno - 1).  An entry holds heaptid, nbrtid, level, flags,
 * filter_val and the SQ8 scale/offset/code.
 *
 * The flat layout replaces the dshash the design doc sketched: a lookup is
 * two dependent loads with NO lock and NO hashing, which matters because the
 * scan does one lookup per discovered neighbor (measured at 60K/dim=128:
 * dshash_find + per-entry dsa pointer chase cost ~0.9 us per discovery and
 * put cache-mode at ~2.5x inline-mode latency; the flat directory is the
 * same access pattern as the inline path's in-page entry read).  M2's write
 * path fits the layout: upsert writes a slot in place, vacuum invalidation
 * clears the PRESENT flag, both under entry versioning.
 *
 * Loading is lazy and non-blocking: the first scan that finds a slot EMPTY
 * CASes it to LOADING and walks the index main fork, quantizing every
 * element tuple's fp32 vector with acorn_sq8_encode (the same encoder the
 * inline build uses, so codes are bit-identical to inline codes).  Other
 * scans never wait: while LOADING and on any miss they use the element-page
 * fallback (design G4 — correctness never depends on cache state).  If the
 * pg_acorn.code_cache_size budget is exceeded mid-load the slot becomes
 * PARTIAL: present blocks serve, misses fall back.
 *
 * Locking contract: dir->lock (exclusive) protects slot claiming and handle
 * publication, (shared) handle reads.  Slot state transitions go through
 * the atomic only: EMPTY -> LOADING by CAS (single loader), LOADING ->
 * READY|PARTIAL by the loader after a write barrier.  Per-slot storage
 * needs no locks at all: it has a single writer (the LOADING owner) and is
 * immutable once the state leaves LOADING; readers attach only after
 * observing READY/PARTIAL.
 *
 * M1 caveats (fixed in M2 — do not rely on the cache for correctness):
 *   - no insert upsert: elements inserted after the load are misses, which
 *     fall back to the element-page read (correct, just slower);
 *   - no vacuum invalidation: an index TID reused after VACUUM could serve
 *     a stale entry.  Acceptable while pg_acorn.scan_code_cache defaults
 *     off; M2 adds ambulkdelete invalidation + entry versioning.  Stale
 *     deleted flags / heaptids are partially masked by the exact re-rank,
 *     which re-reads the element page before emission.
 */

#include "postgres.h"

#include "miscadmin.h"
#include "port/atomics.h"
#include "storage/bufmgr.h"
#include "storage/dsm_registry.h"
#include "storage/itemid.h"
#include "storage/lwlock.h"
#include "utils/dsa.h"
#include "utils/hsearch.h"
#include "utils/memutils.h"
#include "utils/rel.h"

#include "pg_acorn.h"
#include "hnsw_compat.h"
#include "acorn_t2_page.h"
#include "acorn_dist.h"
#include "acorn_codecache.h"

/* -----------------------------------------------------------------------
 * Shared directory (named DSM segment)
 * ----------------------------------------------------------------------- */

#define ACORN_CC_SEGMENT_NAME	"pg_acorn_cc"
#define ACORN_CC_NSLOTS			64

/* slot states */
#define ACORN_CC_STATE_EMPTY	0
#define ACORN_CC_STATE_LOADING	1
#define ACORN_CC_STATE_READY	2
#define ACORN_CC_STATE_PARTIAL	3

/* fixed entry stride: keeps filter_val 8-aligned in the per-page array */
#define ACORN_CC_ENTRY_STRIDE(dim) \
	MAXALIGN(offsetof(AcornCodeCacheEntry, code) + (dim))

/* per-page entry array: 8-byte header, then nslots entries of fixed stride */
typedef struct AcornCCPageHdr
{
	uint16		nslots;			/* entries indexed by offno-1, < nslots+1 */
	uint16		reserved;
	uint32		reserved2;
} AcornCCPageHdr;

#define ACORN_CC_PAGE_SIZE(nslots, stride) \
	(sizeof(AcornCCPageHdr) + (Size) (nslots) * (stride))
#define ACORN_CC_PAGE_ENTRY(hdr, offno, stride) \
	((AcornCodeCacheEntry *) ((char *) (hdr) + sizeof(AcornCCPageHdr) + \
							  (Size) ((offno) - 1) * (stride)))

typedef struct AcornCodeCacheSlot
{
	Oid				dboid;			/* InvalidOid = slot unclaimed */
	RelFileNumber	relnumber;
	pg_atomic_uint32 state;			/* ACORN_CC_STATE_* */
	uint32			generation;		/* bumped per (re)load */
	uint32			nelems;
	uint64			bytes;			/* accounted bytes */
	int				dim;			/* code length; stride = 32 + dim aligned */
	uint32			nblocks;		/* length of the block directory */
	dsa_handle		area_handle;
	dsa_pointer		blocks;			/* dsa_pointer[nblocks] page arrays */
} AcornCodeCacheSlot;

typedef struct AcornCodeCacheDirectory
{
	LWLock			lock;
	int				lock_tranche;
	int				dsa_tranche;
	pg_atomic_uint64 total_bytes;	/* global accounted bytes, all slots */
	AcornCodeCacheSlot slots[ACORN_CC_NSLOTS];
} AcornCodeCacheDirectory;

/* -----------------------------------------------------------------------
 * Backend-local state
 * ----------------------------------------------------------------------- */

struct AcornCodeCacheScan
{
	dsa_area	   *area;
	dsa_pointer    *blocks;			/* mapped block directory */
	uint32			nblocks;
	Size			stride;
};

typedef struct AcornCCAttachKey
{
	Oid				dboid;
	RelFileNumber	relnumber;
} AcornCCAttachKey;

typedef struct AcornCCAttachEntry
{
	AcornCCAttachKey key;			/* must be first (HASH_BLOBS) */
	AcornCodeCacheScan scan;
} AcornCCAttachEntry;

static AcornCodeCacheDirectory *acorn_cc_dir = NULL;
static HTAB *acorn_cc_attached = NULL;

/* -----------------------------------------------------------------------
 * Directory attach
 * ----------------------------------------------------------------------- */

static void
acorn_cc_dir_init(void *ptr)
{
	AcornCodeCacheDirectory *dir = (AcornCodeCacheDirectory *) ptr;

	memset(dir, 0, sizeof(AcornCodeCacheDirectory));
	dir->lock_tranche = LWLockNewTrancheId();
	dir->dsa_tranche = LWLockNewTrancheId();
	LWLockInitialize(&dir->lock, dir->lock_tranche);
	pg_atomic_init_u64(&dir->total_bytes, 0);
	for (int i = 0; i < ACORN_CC_NSLOTS; i++)
		pg_atomic_init_u32(&dir->slots[i].state, ACORN_CC_STATE_EMPTY);
}

static AcornCodeCacheDirectory *
acorn_cc_get_dir(void)
{
	if (acorn_cc_dir == NULL)
	{
		bool		found;

		acorn_cc_dir = (AcornCodeCacheDirectory *)
			GetNamedDSMSegment(ACORN_CC_SEGMENT_NAME,
							   sizeof(AcornCodeCacheDirectory),
							   acorn_cc_dir_init, &found);
		LWLockRegisterTranche(acorn_cc_dir->lock_tranche, "pg_acorn_cc_dir");
		LWLockRegisterTranche(acorn_cc_dir->dsa_tranche, "pg_acorn_cc_dsa");
	}
	return acorn_cc_dir;
}

/*
 * Find the slot for (dboid, relnumber), claiming a free one if absent.
 * Returns NULL when the directory is full (no cache for this index).
 */
static AcornCodeCacheSlot *
acorn_cc_slot_lookup(AcornCodeCacheDirectory *dir,
					 Oid dboid, RelFileNumber relnumber)
{
	AcornCodeCacheSlot *slot = NULL;
	AcornCodeCacheSlot *freeslot = NULL;

	LWLockAcquire(&dir->lock, LW_EXCLUSIVE);
	for (int i = 0; i < ACORN_CC_NSLOTS; i++)
	{
		AcornCodeCacheSlot *s = &dir->slots[i];

		if (s->dboid == dboid && s->relnumber == relnumber)
		{
			slot = s;
			break;
		}
		if (freeslot == NULL && s->dboid == InvalidOid)
			freeslot = s;
	}
	if (slot == NULL && freeslot != NULL)
	{
		freeslot->dboid = dboid;
		freeslot->relnumber = relnumber;
		slot = freeslot;
	}
	LWLockRelease(&dir->lock);
	return slot;
}

/* -----------------------------------------------------------------------
 * Backend-local attachment cache
 * ----------------------------------------------------------------------- */

static void
acorn_cc_attach_init(void)
{
	HASHCTL		info;

	if (acorn_cc_attached != NULL)
		return;
	memset(&info, 0, sizeof(info));
	info.keysize = sizeof(AcornCCAttachKey);
	info.entrysize = sizeof(AcornCCAttachEntry);
	info.hcxt = TopMemoryContext;
	acorn_cc_attached = hash_create("acorn_codecache attachments", 8,
									&info,
									HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
}

static AcornCCAttachEntry *
acorn_cc_attach_find(Oid dboid, RelFileNumber relnumber, bool *found)
{
	AcornCCAttachKey key;

	acorn_cc_attach_init();
	memset(&key, 0, sizeof(key));
	key.dboid = dboid;
	key.relnumber = relnumber;
	return (AcornCCAttachEntry *)
		hash_search(acorn_cc_attached, &key, HASH_FIND, found);
}

static AcornCCAttachEntry *
acorn_cc_attach_store(Oid dboid, RelFileNumber relnumber,
					  dsa_area *area, dsa_pointer *blocks,
					  uint32 nblocks, Size stride)
{
	AcornCCAttachKey key;
	AcornCCAttachEntry *e;
	bool		found;

	acorn_cc_attach_init();
	memset(&key, 0, sizeof(key));
	key.dboid = dboid;
	key.relnumber = relnumber;
	e = (AcornCCAttachEntry *)
		hash_search(acorn_cc_attached, &key, HASH_ENTER, &found);
	e->scan.area = area;
	e->scan.blocks = blocks;
	e->scan.nblocks = nblocks;
	e->scan.stride = stride;
	return e;
}

/*
 * Attach this backend to a READY/PARTIAL slot's DSA area and map the block
 * directory.  Attachments are pinned for the backend's lifetime
 * (dsa_pin_mapping) and cached, so each backend pays the attach cost once
 * per index.
 */
static AcornCodeCacheScan *
acorn_cc_attach(AcornCodeCacheDirectory *dir, AcornCodeCacheSlot *slot,
				Oid dboid, RelFileNumber relnumber)
{
	AcornCCAttachEntry *e;
	bool		found;
	dsa_handle	area_handle;
	dsa_pointer blocks_dp;
	uint32		nblocks;
	int			dim;
	dsa_area   *area;
	dsa_pointer *blocks;
	MemoryContext old;

	e = acorn_cc_attach_find(dboid, relnumber, &found);
	if (found)
		return &e->scan;

	LWLockAcquire(&dir->lock, LW_SHARED);
	area_handle = slot->area_handle;
	blocks_dp = slot->blocks;
	nblocks = slot->nblocks;
	dim = slot->dim;
	LWLockRelease(&dir->lock);

	old = MemoryContextSwitchTo(TopMemoryContext);
	area = dsa_attach(area_handle);
	dsa_pin_mapping(area);
	MemoryContextSwitchTo(old);
	blocks = (dsa_pointer *) dsa_get_address(area, blocks_dp);

	e = acorn_cc_attach_store(dboid, relnumber, area, blocks, nblocks,
							  ACORN_CC_ENTRY_STRIDE(dim));
	return &e->scan;
}

/* -----------------------------------------------------------------------
 * Loader
 * ----------------------------------------------------------------------- */

/*
 * Load all element tuples of `index` into the slot's block directory.
 * Caller owns the LOADING claim (won the EMPTY -> LOADING CAS).  On return
 * the slot is READY, or PARTIAL when the budget ran out; on error the slot
 * is published PARTIAL with whatever loaded (a block's entry array is fully
 * built before its directory pointer is set, so partial content is always
 * servable) and the error is re-thrown.
 */
static void
acorn_cc_load(AcornCodeCacheDirectory *dir, AcornCodeCacheSlot *slot,
			  Relation index, int dim)
{
	dsa_area   *area;
	MemoryContext old;
	uint64		budget = (uint64) acorn_code_cache_size_mb * 1024 * 1024;
	uint64		other_bytes;
	Size		stride = ACORN_CC_ENTRY_STRIDE(dim);
	BlockNumber nblocks = RelationGetNumberOfBlocks(index);
	dsa_pointer blocks_dp;
	dsa_pointer *blocks;
	volatile uint64 bytes = 0;
	volatile uint32 nelems = 0;
	volatile uint32 final_state = ACORN_CC_STATE_READY;

	/* backend-local control structs must outlive the transaction */
	old = MemoryContextSwitchTo(TopMemoryContext);
	area = dsa_create(dir->dsa_tranche);
	dsa_pin(area);
	dsa_pin_mapping(area);
	MemoryContextSwitchTo(old);

	blocks_dp = dsa_allocate0(area, (Size) nblocks * sizeof(dsa_pointer));
	blocks = (dsa_pointer *) dsa_get_address(area, blocks_dp);
	bytes += (Size) nblocks * sizeof(dsa_pointer);

	/* publish handles before the state can leave LOADING */
	LWLockAcquire(&dir->lock, LW_EXCLUSIVE);
	slot->area_handle = dsa_get_handle(area);
	slot->blocks = blocks_dp;
	slot->nblocks = nblocks;
	slot->dim = dim;
	slot->generation++;
	LWLockRelease(&dir->lock);

	/* the loading backend is attached by construction */
	acorn_cc_attach_store(index->rd_locator.dbOid,
						  index->rd_locator.relNumber, area, blocks,
						  nblocks, stride);

	other_bytes = pg_atomic_read_u64(&dir->total_bytes);

	PG_TRY();
	{
		for (BlockNumber blkno = HNSW_METAPAGE_BLKNO + 1;
			 blkno < nblocks && final_state == ACORN_CC_STATE_READY;
			 blkno++)
		{
			Buffer		buf;
			Page		page;
			OffsetNumber maxoff;
			dsa_pointer page_dp = InvalidDsaPointer;
			AcornCCPageHdr *ph = NULL;
			Size		page_sz = 0;
			uint32		page_elems = 0;

			CHECK_FOR_INTERRUPTS();

			buf = ReadBuffer(index, blkno);
			LockBuffer(buf, BUFFER_LOCK_SHARE);
			page = BufferGetPage(buf);
			maxoff = PageGetMaxOffsetNumber(page);

			for (OffsetNumber offno = FirstOffsetNumber; offno <= maxoff;
				 offno = OffsetNumberNext(offno))
			{
				ItemId		iid = PageGetItemId(page, offno);
				AcornT2ElementTuple etup;
				AcornPgVector *vec;
				AcornCodeCacheEntry *e;

				if (!ItemIdIsUsed(iid) || !ItemIdHasStorage(iid))
					continue;
				etup = (AcornT2ElementTuple) PageGetItem(page, iid);
				if (etup->type != HNSW_ELEMENT_TUPLE_TYPE)
					continue;	/* skip meta/neighbor/inline-cont tuples */
				vec = (AcornPgVector *) AcornT2ElementTupleGetVector(etup);
				if ((int) vec->dim != dim)
					continue;	/* defensive: never cache a malformed code */

				if (ph == NULL)
				{
					/* lazy per-page array, sized for this page's offsets */
					page_sz = ACORN_CC_PAGE_SIZE(maxoff, stride);
					if (other_bytes + bytes + page_sz > budget)
					{
						final_state = ACORN_CC_STATE_PARTIAL;
						break;
					}
					page_dp = dsa_allocate_extended(area, page_sz,
													DSA_ALLOC_NO_OOM |
													DSA_ALLOC_ZERO);
					if (!DsaPointerIsValid(page_dp))
					{
						final_state = ACORN_CC_STATE_PARTIAL;
						break;
					}
					ph = (AcornCCPageHdr *) dsa_get_address(area, page_dp);
					ph->nslots = maxoff;
					bytes += page_sz;
				}

				e = ACORN_CC_PAGE_ENTRY(ph, offno, stride);
				e->heaptid = etup->heaptids[0];
				e->nbrtid = etup->neighbortid;
				e->level = etup->level;
				e->flags = ACORN_CC_PRESENT |
					((etup->deleted != 0) ? ACORN_CC_DELETED : 0);
				e->filter_val = etup->filter_val;
				acorn_sq8_encode(dim, vec->x, e->code, &e->scale, &e->offset);
				page_elems++;
			}

			UnlockReleaseBuffer(buf);

			/*
			 * Publish the block only after its entries are complete, so a
			 * PARTIAL slot (budget stop or error) never exposes a torn
			 * page array.  The array is allocated lazily on the page's
			 * first element tuple, so page_elems > 0 whenever one exists.
			 */
			if (page_elems > 0)
			{
				blocks[blkno] = page_dp;
				nelems += page_elems;
			}
		}
	}
	PG_CATCH();
	{
		/*
		 * Publish what loaded so far as PARTIAL: every published block is
		 * complete, and misses fall back (G4).  The slot never returns to
		 * EMPTY in M1, so the create-vs-reuse branch does not exist.
		 */
		slot->nelems = nelems;
		slot->bytes = bytes;
		pg_atomic_fetch_add_u64(&dir->total_bytes, bytes);
		pg_write_barrier();
		pg_atomic_write_u32(&slot->state, ACORN_CC_STATE_PARTIAL);
		PG_RE_THROW();
	}
	PG_END_TRY();

	slot->nelems = nelems;
	slot->bytes = bytes;
	pg_atomic_fetch_add_u64(&dir->total_bytes, bytes);
	pg_write_barrier();
	pg_atomic_write_u32(&slot->state, final_state);
}

/* -----------------------------------------------------------------------
 * Public API
 * ----------------------------------------------------------------------- */

AcornCodeCacheScan *
acorn_codecache_begin_scan(Relation index, int dim)
{
	AcornCodeCacheDirectory *dir;
	AcornCodeCacheSlot *slot;
	Oid			dboid = index->rd_locator.dbOid;
	RelFileNumber relnumber = index->rd_locator.relNumber;
	uint32		state;
	AcornCCAttachEntry *att;
	bool		found;

	if (!acorn_scan_code_cache || acorn_code_cache_size_mb <= 0 || dim <= 0)
		return NULL;

	/*
	 * Steady state: an existing attachment stays valid for the backend's
	 * lifetime in M1 (no eviction, slot states never regress, REINDEX
	 * changes the relfilenumber key), so repeat scans skip the directory
	 * lock entirely.
	 */
	att = acorn_cc_attach_find(dboid, relnumber, &found);
	if (found)
		return &att->scan;

	dir = acorn_cc_get_dir();
	slot = acorn_cc_slot_lookup(dir, dboid, relnumber);
	if (slot == NULL)
		return NULL;			/* directory full: run at non-inline speed */

	state = pg_atomic_read_u32(&slot->state);
	if (state == ACORN_CC_STATE_EMPTY)
	{
		if (pg_atomic_compare_exchange_u32(&slot->state, &state,
										   ACORN_CC_STATE_LOADING))
		{
			acorn_cc_load(dir, slot, index, dim);
			state = pg_atomic_read_u32(&slot->state);
		}
		/* lost the CAS: `state` now holds the winner's value */
	}

	/* Readers never wait: a slot LOADING elsewhere means no cache this scan */
	if (state != ACORN_CC_STATE_READY && state != ACORN_CC_STATE_PARTIAL)
		return NULL;
	pg_read_barrier();

	if (slot->dim != dim)
		return NULL;			/* defensive: stale slot from a prior life */

	return acorn_cc_attach(dir, slot, dboid, relnumber);
}

const AcornCodeCacheEntry *
acorn_codecache_lookup(AcornCodeCacheScan *cc,
					   BlockNumber blkno, OffsetNumber offno)
{
	dsa_pointer page_dp;
	AcornCCPageHdr *ph;
	const AcornCodeCacheEntry *e;

	if (blkno >= cc->nblocks)
		return NULL;
	page_dp = cc->blocks[blkno];
	if (!DsaPointerIsValid(page_dp))
		return NULL;
	ph = (AcornCCPageHdr *) dsa_get_address(cc->area, page_dp);
	if (offno < 1 || offno > ph->nslots)
		return NULL;
	e = ACORN_CC_PAGE_ENTRY(ph, offno, cc->stride);
	if ((e->flags & ACORN_CC_PRESENT) == 0)
		return NULL;

	/*
	 * Safe to dereference after return: M1 entries are immutable and never
	 * freed (no eviction, no vacuum invalidation).  M2 entry versioning
	 * revisits this contract.
	 */
	return e;
}
