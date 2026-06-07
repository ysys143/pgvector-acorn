\echo Use "CREATE EXTENSION pg_acorn" to load this file. \quit

-- acorn_hnsw index access method
CREATE FUNCTION acorn_hnsw_handler(internal)
    RETURNS index_am_handler
    AS 'MODULE_PATHNAME'
    LANGUAGE C;

CREATE ACCESS METHOD acorn_hnsw TYPE INDEX HANDLER acorn_hnsw_handler;

COMMENT ON ACCESS METHOD acorn_hnsw IS
    'ACORN-HNSW: filterable approximate nearest neighbor index';

-- GUCs (loaded via _PG_init, declared here for documentation)
-- pg_acorn.enable_hook     boolean  default true   (Tier 1 hook)
-- pg_acorn.default_gamma   integer  default 1      (ACORN-1 by default)
