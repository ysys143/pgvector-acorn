/*
 * acorn_dist.h — direct C distance kernels (fmgr bypass)
 *
 * For known pgvector distance support functions the Tier 2 scan calls these
 * directly on the raw float arrays instead of going through fmgr per node.
 * Loop bodies and compile flags replicate pgvector 0.8.0 (see Makefile rule
 * for src/acorn_dist.o) so results are bit-identical to the fmgr path.
 */

#ifndef ACORN_DIST_H
#define ACORN_DIST_H

/* Signature shared by all direct kernels: distance(dim, stored, query). */
typedef double (*AcornDistFn) (int dim, const float *ax, const float *bx);

/* vector_l2_squared_distance(vector, vector) */
extern double acorn_dist_l2sq(int dim, const float *ax, const float *bx);

/* vector_negative_inner_product(vector, vector) — also cosine opclass */
extern double acorn_dist_neg_ip(int dim, const float *ax, const float *bx);

#endif							/* ACORN_DIST_H */
