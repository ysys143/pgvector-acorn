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
