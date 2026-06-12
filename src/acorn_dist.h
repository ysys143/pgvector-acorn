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

/* -----------------------------------------------------------------------
 * SQ8 asymmetric kernels (vector co-location, acorn_inline_vectors)
 *
 * Stored side is a uint8 code with per-vector affine dequantization
 * x[i] ~ offset + scale * code[i]; the query side stays float.  Used for
 * APPROXIMATE candidate ordering only — emitted results are re-ranked with
 * the exact kernels above.
 * ----------------------------------------------------------------------- */

typedef double (*AcornSq8DistFn) (int dim, const uint8 *code,
								  float scale, float offset, const float *qx);

extern double acorn_dist_l2sq_sq8(int dim, const uint8 *code,
								  float scale, float offset, const float *qx);
extern double acorn_dist_neg_ip_sq8(int dim, const uint8 *code,
									float scale, float offset, const float *qx);

/* Encode one float vector into SQ8 codes + per-vector scale/offset. */
extern void acorn_sq8_encode(int dim, const float *x, uint8 *code_out,
							 float *scale_out, float *offset_out);

/* Dequantize codes back into floats (fmgr fallback for unknown opclasses). */
extern void acorn_sq8_decode(int dim, const uint8 *code,
							 float scale, float offset, float *x_out);

#endif							/* ACORN_DIST_H */
