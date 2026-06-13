\echo Use "CREATE EXTENSION pg_acorn" to load this file. \quit

-- acorn_hnsw index access method
CREATE FUNCTION acorn_hnsw_handler(internal)
    RETURNS index_am_handler
    AS 'MODULE_PATHNAME'
    LANGUAGE C;

CREATE ACCESS METHOD acorn_hnsw TYPE INDEX HANDLER acorn_hnsw_handler;

COMMENT ON ACCESS METHOD acorn_hnsw IS
    'ACORN-HNSW: filterable approximate nearest neighbor index';

-- Operator classes for acorn_hnsw.
--
-- These mirror pgvector's hnsw opclasses exactly (same operators and support
-- functions) but bind them to the acorn_hnsw AM.  Because acorn_hnsw writes
-- pages in pgvector's on-disk format, the shared traversal in acorn_scan.c
-- resolves the distance kernel via index->rd_support[0] = FUNCTION 1 here.
--
-- vector_negative_inner_product / vector_l2_squared_distance / vector_norm /
-- l1_distance are provided by the `vector` extension (required dependency).

CREATE OPERATOR CLASS vector_l2_ops
    DEFAULT FOR TYPE vector USING acorn_hnsw AS
    OPERATOR 1 <-> (vector, vector) FOR ORDER BY float_ops,
    FUNCTION 1 vector_l2_squared_distance(vector, vector);

CREATE OPERATOR CLASS vector_ip_ops
    FOR TYPE vector USING acorn_hnsw AS
    OPERATOR 1 <#> (vector, vector) FOR ORDER BY float_ops,
    FUNCTION 1 vector_negative_inner_product(vector, vector);

CREATE OPERATOR CLASS vector_cosine_ops
    FOR TYPE vector USING acorn_hnsw AS
    OPERATOR 1 <=> (vector, vector) FOR ORDER BY float_ops,
    FUNCTION 1 vector_negative_inner_product(vector, vector),
    FUNCTION 2 vector_norm(vector);

CREATE OPERATOR CLASS vector_l1_ops
    FOR TYPE vector USING acorn_hnsw AS
    OPERATOR 1 <+> (vector, vector) FOR ORDER BY float_ops,
    FUNCTION 1 l1_distance(vector, vector);

-- Scalar filter opclasses for acorn_hnsw (Tier 2 in-filter ACORN).
--
-- A multi-column acorn_hnsw index (embedding vector_*_ops, attr <scalar>_acorn_ops)
-- lets the planner pass a scalar comparison (e.g. bucket < N) as an indexable
-- ScanKey on the second column; the AM evaluates it against the value stored
-- inline in each node during traversal (no heap fetch).  Strategy numbers follow
-- btree (1=<, 2=<=, 3==, 4=>=, 5=>); FUNCTION 1 is the btree-style 3-way compare.

CREATE OPERATOR CLASS int4_acorn_ops
    FOR TYPE int4 USING acorn_hnsw AS
    OPERATOR 1 < (int4, int4),
    OPERATOR 2 <= (int4, int4),
    OPERATOR 3 = (int4, int4),
    OPERATOR 4 >= (int4, int4),
    OPERATOR 5 > (int4, int4),
    FUNCTION 1 btint4cmp(int4, int4);

CREATE OPERATOR CLASS text_acorn_ops
    FOR TYPE text USING acorn_hnsw AS
    OPERATOR 1 < (text, text),
    OPERATOR 2 <= (text, text),
    OPERATOR 3 = (text, text),
    OPERATOR 4 >= (text, text),
    OPERATOR 5 > (text, text),
    FUNCTION 1 bttextcmp(text, text);

-- Cross-type comparisons for int4_acorn_ops.
--
-- Drivers using the extended protocol often bind small integer constants as
-- int2 or int8 (psycopg sends Python ints as smallint when they fit).
-- Without family membership for the cross-type operators, a qual like
-- `bucket < $1::smallint` cannot be pushed down as an index cond and the
-- scan silently degrades to the unfiltered post-filter path.  The AM
-- evaluates ScanKeys via the operator's own proc (int42lt etc.), so no C
-- changes are needed.

ALTER OPERATOR FAMILY int4_acorn_ops USING acorn_hnsw ADD
    OPERATOR 1 < (int4, int2),
    OPERATOR 2 <= (int4, int2),
    OPERATOR 3 = (int4, int2),
    OPERATOR 4 >= (int4, int2),
    OPERATOR 5 > (int4, int2),
    OPERATOR 1 < (int4, int8),
    OPERATOR 2 <= (int4, int8),
    OPERATOR 3 = (int4, int8),
    OPERATOR 4 >= (int4, int8),
    OPERATOR 5 > (int4, int8),
    FUNCTION 1 (int4, int2) btint42cmp(int4, int2),
    FUNCTION 1 (int4, int8) btint48cmp(int4, int8);

-- Shared-memory SQ8 code cache observability + admin (M3).
--
-- The cache is per-index shared state in a named DSM segment; these read it
-- under the directory lock.  VOLATILE (the cache mutates under DML/eviction)
-- and PARALLEL RESTRICTED (touches process-shared DSM/LWLock state that is
-- not safe to enter from a parallel worker without the directory mapping).

CREATE FUNCTION pg_acorn_code_cache_stats(
    OUT dboid                   oid,
    OUT relfilenumber           int8,
    OUT indexrelid              regclass,
    OUT state                   text,
    OUT nelems                  int8,
    OUT bytes                   int8,
    OUT generation              int8,
    OUT lastused                int8,
    OUT blocks_retired_pending  int4)
    RETURNS SETOF record
    AS 'MODULE_PATHNAME', 'pg_acorn_code_cache_stats'
    LANGUAGE C VOLATILE PARALLEL RESTRICTED;

COMMENT ON FUNCTION pg_acorn_code_cache_stats() IS
    'One row per occupied acorn_hnsw code-cache slot (shared memory).';

CREATE FUNCTION pg_acorn_code_cache_summary(
    OUT total_bytes   int8,
    OUT budget_bytes  int8,
    OUT n_slots       int4,
    OUT n_occupied    int4)
    RETURNS record
    AS 'MODULE_PATHNAME', 'pg_acorn_code_cache_summary'
    LANGUAGE C VOLATILE PARALLEL RESTRICTED;

COMMENT ON FUNCTION pg_acorn_code_cache_summary() IS
    'Total acorn_hnsw code-cache bytes resident vs the configured budget.';

CREATE FUNCTION pg_acorn_code_cache_evict(index regclass)
    RETURNS boolean
    AS 'MODULE_PATHNAME', 'pg_acorn_code_cache_evict'
    LANGUAGE C VOLATILE PARALLEL UNSAFE;

COMMENT ON FUNCTION pg_acorn_code_cache_evict(regclass) IS
    'Force-evict one index''s acorn_hnsw code-cache slot; true if it was present.';

REVOKE ALL ON FUNCTION pg_acorn_code_cache_evict(regclass) FROM PUBLIC;

CREATE FUNCTION pg_acorn_code_cache_reset()
    RETURNS integer
    AS 'MODULE_PATHNAME', 'pg_acorn_code_cache_reset'
    LANGUAGE C VOLATILE PARALLEL UNSAFE;

COMMENT ON FUNCTION pg_acorn_code_cache_reset() IS
    'Force-evict every evictable acorn_hnsw code-cache slot (incl. orphans from '
    'dropped indexes); returns the count evicted.';

REVOKE ALL ON FUNCTION pg_acorn_code_cache_reset() FROM PUBLIC;

-- GUCs (loaded via _PG_init, declared here for documentation)
-- pg_acorn.enable_hook       boolean  default true   (Tier 1 hook)
-- pg_acorn.default_gamma     integer  default 1      (ACORN-1 by default)
-- pg_acorn.scan_code_cache   boolean  default true   (graduated: non-inline
--                                                      indexes are cache-served;
--                                                      a miss/0-budget falls back)
-- pg_acorn.code_cache_size   integer  default 512MB  (shared budget; 0 disables)
