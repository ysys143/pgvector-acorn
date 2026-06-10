/*
 * acorn_dist.c — direct C distance kernels (fmgr bypass)
 *
 * The loop bodies are verbatim ports of pgvector 0.8.0 src/vector.c
 * (VectorL2SquaredDistance, VectorInnerProduct): float accumulator,
 * widened to double only at return — exactly what the fmgr-called
 * functions compute.  This file is compiled with the same extra flags
 * pgvector uses (-march=native -ftree-vectorize -fassociative-math
 * -fno-signed-zeros -fno-trapping-math; see Makefile) so the
 * auto-vectorized summation order matches pgvector's binary and the
 * results are numerically identical to the fmgr path.
 */

#include "postgres.h"

#include "acorn_dist.h"

/* pgvector 0.8.0 VectorL2SquaredDistance */
double
acorn_dist_l2sq(int dim, const float *ax, const float *bx)
{
	float		distance = 0.0;

	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
	{
		float		diff = ax[i] - bx[i];

		distance += diff * diff;
	}

	return (double) distance;
}

/* pgvector 0.8.0 vector_negative_inner_product: -(double) VectorInnerProduct */
double
acorn_dist_neg_ip(int dim, const float *ax, const float *bx)
{
	float		distance = 0.0;

	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
		distance += ax[i] * bx[i];

	return (double) -distance;
}

/* -----------------------------------------------------------------------
 * SQ8 asymmetric kernels (vector co-location)
 *
 * Stored codes dequantize as offset + scale * code[i]; the query side is
 * float.  Same accumulator discipline as the exact kernels so the compiler
 * auto-vectorizes the loops under the Makefile's pgvector flags.
 * ----------------------------------------------------------------------- */

double
acorn_dist_l2sq_sq8(int dim, const uint8 *code, float scale, float offset,
					const float *qx)
{
	float		distance = 0.0;

	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
	{
		float		diff = qx[i] - (offset + scale * (float) code[i]);

		distance += diff * diff;
	}

	return (double) distance;
}

double
acorn_dist_neg_ip_sq8(int dim, const uint8 *code, float scale, float offset,
					  const float *qx)
{
	float		distance = 0.0;

	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
		distance += qx[i] * (offset + scale * (float) code[i]);

	return (double) -distance;
}

/*
 * Affine SQ8 encoder: code[i] = round((x[i] - min) / scale), scale =
 * (max - min) / 255.  Constant vectors encode with scale 0 (codes all 0,
 * dequantizing back to the constant via offset).
 */
void
acorn_sq8_encode(int dim, const float *x, uint8 *code_out,
				 float *scale_out, float *offset_out)
{
	float		lo = x[0];
	float		hi = x[0];
	float		scale;
	float		inv;

	for (int i = 1; i < dim; i++)
	{
		if (x[i] < lo)
			lo = x[i];
		if (x[i] > hi)
			hi = x[i];
	}

	scale = (hi - lo) / 255.0f;
	inv = (scale > 0.0f) ? 1.0f / scale : 0.0f;

	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
	{
		float		q = (x[i] - lo) * inv + 0.5f;

		code_out[i] = (uint8) q;	/* q in [0, 255.5); truncation == round */
	}

	*scale_out = scale;
	*offset_out = lo;
}

void
acorn_sq8_decode(int dim, const uint8 *code, float scale, float offset,
				 float *x_out)
{
	/* Auto-vectorized */
	for (int i = 0; i < dim; i++)
		x_out[i] = offset + scale * (float) code[i];
}
