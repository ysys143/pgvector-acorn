/*
 * acorn_t2_page.h — Tier 2 (acorn_hnsw) own on-disk element tuple format
 *
 * Tier 2 diverges from pgvector's hnsw element format by storing an inline
 * filter column value (filter_val) in each element tuple.  This enables
 * in-filter ACORN traversal — the AM evaluates scalar predicates directly
 * against the stored value without any heap fetch.
 *
 * Neighbor tuples and all page / meta structures are inherited from
 * hnsw_compat.h unchanged — EXCEPT when the index is built with the
 * acorn_inline_vectors reloption (vector co-location, see below), which
 * appends per-neighbor payload data after the neighbor tuple's TID array.
 *
 * Tier 1 (acorn_hook.c, acorn_scan.c batch path) reads pgvector's own HNSW
 * pages via hnsw_compat.h.  Do not mix these two element formats.
 */

#ifndef ACORN_T2_PAGE_H
#define ACORN_T2_PAGE_H

#include "hnsw_compat.h"	/* neighbor tuple, page opaque, meta, all macros */

/* -----------------------------------------------------------------------
 * Tier-2 neighbor-array geometry (additive acorn_payload_m).
 *
 * pgvector's symmetric layout stores HnswGetLayerM(m, 0) = 2*m slots at
 * layer 0 and m at every upper layer, for (level+2)*m total — see the macros
 * in hnsw_compat.h.  Tier-2 generalizes layer 0 to an INDEPENDENT payload
 * half: global_m global-nearest slots + payload_m payload slots, so
 *
 *   layer-0 width   = global_m + payload_m
 *   total slots     = (level+1)*global_m + payload_m
 *   level recovery  = (count - payload_m) / global_m - 1
 *
 * global_m stays the per-(upper-)layer stride, so HnswNeighborStart(global_m,
 * level, layer) = (level-layer)*global_m is UNCHANGED — only the layer-0
 * WIDTH grows.  These helpers REDUCE to the pgvector macros exactly when
 * payload_m == global_m (the symmetric / legacy case):
 *
 *   l0_width:       global_m + global_m            = 2*global_m   = HnswGetLayerM(m,0)
 *   total_slots:    (level+1)*global_m + global_m  = (level+2)*global_m
 *   level_from_cnt: (cnt - global_m)/global_m - 1  = cnt/global_m - 2
 *
 * Do NOT touch hnsw_compat.h's macros — Tier-1 / pgvector-compat readers use
 * them.  These live here, Tier-2 only.
 * ----------------------------------------------------------------------- */

/* Layer-0 slot-array width (global half + payload half). */
static inline int
acorn_t2_l0_width(int global_m, int payload_m)
{
	return global_m + payload_m;
}

/* Slots stored for a given layer (layer 0 = l0_width, upper layers = global_m). */
static inline int
acorn_t2_layer_m(int layer, int global_m, int payload_m)
{
	return (layer == 0) ? acorn_t2_l0_width(global_m, payload_m) : global_m;
}

/* Total neighbor TID slots for an element whose highest layer is `level`. */
static inline int
acorn_t2_total_slots(int level, int global_m, int payload_m)
{
	return (level + 1) * global_m + payload_m;
}

/* Recover an element's highest layer from a neighbor tuple's total slot count. */
static inline int
acorn_t2_level_from_count(int count, int global_m, int payload_m)
{
	return (count - payload_m) / global_m - 1;
}

/*
 * On-disk size of a (non-inline) Tier-2 neighbor tuple: header + the
 * acorn_t2_total_slots TID array.  Reduces to HNSW_NEIGHBOR_TUPLE_SIZE(level,
 * m_eff) when payload_m == global_m.
 */
#define ACORN_T2_NEIGHBOR_TUPLE_SIZE(level, global_m, payload_m) \
	MAXALIGN(sizeof(HnswNeighborTupleData) + \
			 (Size) acorn_t2_total_slots((level), (global_m), (payload_m)) * \
			 sizeof(ItemPointerData))

/*
 * Tier 2 element tuple.
 *
 * The header fields through `unused` sit at the same byte offsets as
 * HnswElementTupleData (type, level, deleted, version, heaptids,
 * neighbortid, unused = bytes 0–71).  filter_val is appended at offset 72,
 * pushing the inline vector to offset 80.  For single-column indexes (no
 * filter) filter_val is stored as 0 and never evaluated.
 *
 * filter_val stores a by-value filter column datum: int4, int8, oid, bool.
 * For int4 it is stored as (int64)(Datum)values[1] and compared via
 * btint4cmp using the ScanKey's sk_func.
 */
typedef struct AcornT2ElementTupleData
{
	uint8			type;
	uint8			level;
	uint8			deleted;
	uint8			version;
	ItemPointerData heaptids[HNSW_HEAPTIDS];	/* heap TID(s) */
	ItemPointerData neighbortid;				/* index TID of neighbor tuple */
	uint16			unused;
	int64			filter_val;		/* inline filter column (0 = no filter) */
	/* Vector data follows immediately at sizeof(AcornT2ElementTupleData) */
} AcornT2ElementTupleData;
typedef AcornT2ElementTupleData *AcornT2ElementTuple;

StaticAssertDecl(sizeof(AcornT2ElementTupleData) == 80,
				 "AcornT2ElementTupleData must be 80 bytes (72 header + 8 filter_val)");

/* Access the inline vector — offset 80, not 72 as in pgvector. */
#define AcornT2ElementTupleGetVector(etup) \
	((void *) ((char *)(etup) + sizeof(AcornT2ElementTupleData)))

/* On-disk tuple size including the vector varlena. */
#define ACORN_T2_ELEMENT_TUPLE_SIZE(vsize) \
	MAXALIGN(sizeof(AcornT2ElementTupleData) + (vsize))

/* -----------------------------------------------------------------------
 * Vector co-location (acorn_inline_vectors reloption)
 *
 * The dominant Tier 2 scan cost is one page pin PER DISCOVERED NEIGHBOR to
 * fetch its vector + metadata from its element page.  With inline vectors
 * the layer-0 neighbor list itself carries, per neighbor slot, everything
 * the scan needs at discovery time:
 *
 *   - an SQ8-quantized copy of the neighbor's vector (1 byte/dim, per-vector
 *     scale/offset floats — per-vector scale preserves accuracy; the page
 *     budget is met by chaining instead of degrading the quantizer),
 *   - the neighbor's heaptid, filter_val, deleted flag,
 *   - the neighbor's OWN neighbor-tuple TID + level, so when the node is
 *     later popped for expansion, no element-page read is needed either.
 *
 * Layout: the layer-0 inline entries live in a chain of chunks.  The first
 * chunk is appended to the element's neighbor tuple, after the (unchanged)
 * TID-slot array — readers that only walk TID slots (tier 1, greedy descent,
 * the non-inline scan path) keep working on inline indexes.  Remaining
 * entries spill into dedicated continuation tuples (tuple type 3) linked by
 * the chunk header's `next` TID.
 *
 * Page budget at the design point (dim=128, m=16, acorn_gamma=2 →
 * m_eff=32, layer-0 slots=64): entry stride = MAXALIGN(40+128) = 168 bytes;
 * 64 entries = 10752 bytes > one 8KB page, so the chain is 2 tuples for a
 * level-0 element (46 entries co-located with the 388-byte TID array, 18 in
 * one continuation tuple).  One expansion therefore touches 2 pages total.
 *
 * Consistency contract: entry i describes the edge target stored in layer-0
 * TID slot i.  Writers update the TID slot first, then the inline entry
 * (separate page locks).  Readers validate entry.indextid against the slot
 * TID and fall back to an element-page read on mismatch / invalid flag, so
 * a torn update or crash between the two writes degrades to the non-inline
 * cost for that edge — never to a wrong result.
 * ----------------------------------------------------------------------- */

#define ACORN_T2_INLINE_TUPLE_TYPE	3	/* continuation chunk tuple */

/* entry flags */
#define ACORN_T2_INLINE_VALID		0x01
#define ACORN_T2_INLINE_DELETED		0x02

typedef struct AcornT2InlineEntry
{
	ItemPointerData indextid;	/* edge target's element TID (must match slot) */
	ItemPointerData heaptid;	/* target's primary heap TID */
	ItemPointerData nbrtid;		/* target's own neighbor-tuple TID */
	uint8			level;		/* target's highest layer */
	uint8			flags;		/* ACORN_T2_INLINE_* */
	/* 4 bytes implicit padding (int64 below is 8-aligned at offset 24) */
	int64			filter_val;	/* target's inline filter value */
	float			scale;		/* SQ8: x[i] ~ offset + scale * code[i] */
	float			offset;
	uint8			code[FLEXIBLE_ARRAY_MEMBER];	/* dim bytes */
} AcornT2InlineEntry;

StaticAssertDecl(offsetof(AcornT2InlineEntry, code) == 40,
				 "AcornT2InlineEntry codes must start at byte 40");

/* On-disk stride of one inline entry (8-aligned so filter_val stays aligned) */
#define ACORN_T2_INLINE_ENTRY_SIZE(dim) \
	MAXALIGN(offsetof(AcornT2InlineEntry, code) + (dim))

/*
 * Chunk header — shared by the primary chunk (inside the neighbor tuple)
 * and continuation tuples.  `start` is the first layer-0 slot index covered
 * by this chunk; entries follow immediately, 8-aligned.
 */
typedef struct AcornT2InlineHdrData
{
	uint16			n_here;		/* entries in this chunk */
	uint16			start;		/* first layer-0 slot index covered */
	uint32			entry_size;	/* on-disk entry stride */
	ItemPointerData next;		/* next chunk tuple; invalid = last */
	uint16			reserved;
} AcornT2InlineHdrData;
typedef AcornT2InlineHdrData *AcornT2InlineHdr;

StaticAssertDecl(sizeof(AcornT2InlineHdrData) == 16,
				 "AcornT2InlineHdrData must be 16 bytes");

/* Continuation chunk tuple (tuple type ACORN_T2_INLINE_TUPLE_TYPE) */
typedef struct AcornT2InlineContData
{
	uint8			type;		/* ACORN_T2_INLINE_TUPLE_TYPE */
	uint8			version;
	uint16			unused;
	uint32			unused2;	/* pad: hdr at 8, entries at 24 (8-aligned) */
	AcornT2InlineHdrData hdr;
	/* entries follow at sizeof(AcornT2InlineContData) */
} AcornT2InlineContData;
typedef AcornT2InlineContData *AcornT2InlineCont;

StaticAssertDecl(sizeof(AcornT2InlineContData) == 24,
				 "AcornT2InlineContData must be 24 bytes");

/* Byte offset of the primary chunk header within a neighbor tuple */
#define ACORN_T2_INLINE_HDR_OFFSET(count) \
	MAXALIGN(sizeof(HnswNeighborTupleData) + (count) * sizeof(ItemPointerData))

#define AcornT2NeighborInlineHdr(ntup) \
	((AcornT2InlineHdr) ((char *)(ntup) + ACORN_T2_INLINE_HDR_OFFSET((ntup)->count)))

#define AcornT2InlineHdrEntries(hdr) \
	((char *)(hdr) + sizeof(AcornT2InlineHdrData))

#define AcornT2ContEntries(cont) \
	((char *)(cont) + sizeof(AcornT2InlineContData))

#define AcornT2InlineEntryAt(base, i, esz) \
	((AcornT2InlineEntry *) ((char *)(base) + (Size)(i) * (esz)))

/*
 * Max inline entries co-located with a neighbor tuple whose TID-slot array
 * holds acorn_t2_total_slots(level, global_m, payload_m) slots.  Reduces to
 * the legacy (level+2)*m_eff form when payload_m == global_m.
 */
static inline int
acorn_t2_inline_primary_cap(int level, int global_m, int payload_m,
							Size entry_size)
{
	Size base = ACORN_T2_INLINE_HDR_OFFSET(
					(Size) acorn_t2_total_slots(level, global_m, payload_m))
		+ sizeof(AcornT2InlineHdrData);

	if (base >= HNSW_MAX_SIZE)
		return 0;
	return (int) ((HNSW_MAX_SIZE - base) / entry_size);
}

/* Max inline entries per continuation tuple */
static inline int
acorn_t2_inline_cont_cap(Size entry_size)
{
	return (int) ((HNSW_MAX_SIZE - sizeof(AcornT2InlineContData)) / entry_size);
}

/*
 * On-disk tuple sizes (both are MAXALIGN multiples by construction).  The
 * inline neighbor tuple's TID-slot array holds acorn_t2_total_slots; reduces
 * to (level+2)*m_eff when payload_m == global_m.
 */
#define ACORN_T2_INLINE_NTUP_SIZE(level, global_m, payload_m, n_here, esz) \
	(ACORN_T2_INLINE_HDR_OFFSET( \
		(Size) acorn_t2_total_slots((level), (global_m), (payload_m))) + \
	 sizeof(AcornT2InlineHdrData) + (Size) (n_here) * (esz))

#define ACORN_T2_INLINE_CONT_SIZE(n_here, esz) \
	(sizeof(AcornT2InlineContData) + (Size) (n_here) * (esz))

/* -----------------------------------------------------------------------
 * Tier 2 meta page extension
 *
 * The acorn_inline_vectors layout decision is recorded in the META PAGE at
 * build time (not read from rd_options at scan time): ALTER INDEX ... SET
 * only changes pg_class.reloptions and must not flip how existing pages are
 * interpreted.  The prefix is byte-identical to pgvector's HnswMetaPageData,
 * and indexes built before this field exist read zeros (PageInit zeroes the
 * page) — i.e. inline off.
 * ----------------------------------------------------------------------- */

#define ACORN_T2_META_INLINE_VECTORS	0x0001

typedef struct AcornT2MetaPageData
{
	HnswMetaPageData hnsw;		/* prefix identical to pgvector 0.8.0 */
	uint16		acorn_flags;	/* ACORN_T2_META_* */
	/*
	 * payload_m: resolved layer-0 payload-half width (additive acorn_payload_m).
	 * Sentinel 0 = SYMMETRIC: payload half = global half = meta->hnsw.m, i.e.
	 * the legacy 2*m layer-0 layout.  Stored 0 whenever payload_m == global_m
	 * so pre-payload_m indexes (which read zeros from a PageInit'd page) keep
	 * working with no migration.  0 can therefore never mean "0 payload edges";
	 * that case is acorn_payload_edges = false.
	 */
	uint16		payload_m;
} AcornT2MetaPageData;
typedef AcornT2MetaPageData *AcornT2MetaPage;

#define AcornT2PageGetMeta(page)	((AcornT2MetaPage) PageGetContents(page))

#endif /* ACORN_T2_PAGE_H */
