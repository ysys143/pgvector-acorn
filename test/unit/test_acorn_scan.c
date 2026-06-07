/*
 * test_acorn_scan.c — unit tests for predicate subgraph traversal
 *
 * Compiled as a standalone binary (no PostgreSQL backend required).
 * Tests the ACORN-1 invariant: filter-failing nodes stay in C (traversal
 * queue) but are excluded from W (result set), preserving connectivity.
 *
 * Build:
 *   make -C test/unit test_acorn_scan
 */

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* -----------------------------------------------------------------------
 * Minimal type stubs so acorn_scan logic can be tested without pg headers.
 * The actual acorn_scan.c uses PostgreSQL types; the algorithm logic is
 * extracted here for unit testing.
 * ----------------------------------------------------------------------- */

typedef struct {
	int   id;
	float distance;
	int   matches_filter;  /* 1 = passes predicate, 0 = fails */
} TestElement;

typedef struct {
	int      capacity;
	int      size;
	TestElement *items;
} ResultSet;   /* W: result set */

typedef struct {
	int      capacity;
	int      size;
	TestElement *items;
} CandidateSet; /* C: traversal candidates */

static void rs_add(ResultSet *W, TestElement e) {
	assert(W->size < W->capacity);
	W->items[W->size++] = e;
}

static void cs_add(CandidateSet *C, TestElement e) {
	assert(C->size < C->capacity);
	C->items[C->size++] = e;
}

/*
 * Minimal ACORN-1 invariant: when processing a neighbor,
 * - matches filter  → add to W (result)
 * - fails filter    → add to C only (preserve traversal connectivity)
 */
static void acorn_process_neighbor(TestElement elem,
								   ResultSet *W,
								   CandidateSet *C,
								   int k)
{
	if (elem.matches_filter && W->size < k)
		rs_add(W, elem);
	else
		cs_add(C, elem);
}

/* -----------------------------------------------------------------------
 * Tests
 * ----------------------------------------------------------------------- */

static int pass = 0, fail = 0;

#define CHECK(cond, msg) do { \
	if (cond) { printf("  [OK] %s\n", msg); pass++; } \
	else       { printf("  [FAIL] %s\n", msg); fail++; } \
} while(0)

/* Filter-matching nodes go to W; non-matching go to C */
static void test_basic_routing(void)
{
	printf("test_basic_routing\n");

	TestElement elems[4] = {
		{1, 0.1f, 1},
		{2, 0.2f, 0},   /* fails filter */
		{3, 0.3f, 1},
		{4, 0.4f, 0},   /* fails filter */
	};

	ResultSet W = {4, 0, malloc(4 * sizeof(TestElement))};
	CandidateSet C = {4, 0, malloc(4 * sizeof(TestElement))};

	for (int i = 0; i < 4; i++)
		acorn_process_neighbor(elems[i], &W, &C, 10);

	CHECK(W.size == 2, "two matching nodes in result set");
	CHECK(C.size == 2, "two non-matching nodes kept in traversal queue");
	CHECK(W.items[0].id == 1, "first result is id=1");
	CHECK(W.items[1].id == 3, "second result is id=3");
	CHECK(C.items[0].id == 2, "non-match id=2 kept in C");

	free(W.items);
	free(C.items);
}

/* Result set is capped at k; overflow goes to C */
static void test_k_limit(void)
{
	printf("test_k_limit\n");

	TestElement elems[5] = {
		{1, 0.1f, 1},
		{2, 0.2f, 1},
		{3, 0.3f, 1},   /* k=2, this should go to C */
		{4, 0.4f, 1},
		{5, 0.5f, 0},
	};

	ResultSet W = {5, 0, malloc(5 * sizeof(TestElement))};
	CandidateSet C = {5, 0, malloc(5 * sizeof(TestElement))};
	int k = 2;

	for (int i = 0; i < 5; i++)
		acorn_process_neighbor(elems[i], &W, &C, k);

	CHECK(W.size == 2, "result set capped at k=2");
	CHECK(C.size == 3, "3 overflow/non-matching nodes in C");

	free(W.items);
	free(C.items);
}

/* All nodes fail filter: W empty, all in C (connectivity preserved) */
static void test_all_fail_filter(void)
{
	printf("test_all_fail_filter\n");

	TestElement elems[3] = {
		{1, 0.1f, 0},
		{2, 0.2f, 0},
		{3, 0.3f, 0},
	};

	ResultSet W = {3, 0, malloc(3 * sizeof(TestElement))};
	CandidateSet C = {3, 0, malloc(3 * sizeof(TestElement))};

	for (int i = 0; i < 3; i++)
		acorn_process_neighbor(elems[i], &W, &C, 10);

	CHECK(W.size == 0, "result set empty when all fail filter");
	CHECK(C.size == 3, "all nodes remain in traversal queue for connectivity");

	free(W.items);
	free(C.items);
}

int main(void)
{
	printf("=== acorn_scan unit tests ===\n\n");
	test_basic_routing();
	test_k_limit();
	test_all_fail_filter();
	printf("\n%d passed, %d failed\n", pass, fail);
	return fail > 0 ? 1 : 0;
}
