#ifndef ACORN_AM_H
#define ACORN_AM_H

#include "postgres.h"

/*
 * Tier 2: acorn_hnsw index Access Method
 *
 * Full IndexAmRoutine registration.  Build stores M*gamma neighbors per node
 * (controlled by the acorn_gamma reloption).  Scan delegates to acorn_scan.c.
 */

/* Entry point registered in pg_acorn--0.1.0.sql */
Datum acorn_hnsw_handler(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(acorn_hnsw_handler);

/* reloption defaults */
#define ACORN_DEFAULT_M              16
#define ACORN_DEFAULT_EF_CONSTRUCTION 64
#define ACORN_DEFAULT_GAMMA           1

#endif /* ACORN_AM_H */
