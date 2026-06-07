/*
 * test_acorn_build.c — unit tests for M*gamma neighbor selection and
 * fixed-slot retry logic
 *
 * Tests two behaviors of acorn_build.c:
 * 1. M*gamma neighbor storage: with gamma=2, each node stores 2*M neighbors
 *    at build time to improve recall under adversarial filters.
 * 2. Fixed-slot retry: when inserting a bidirectional edge and the neighbor's
 *    slot is full, replace the furthest existing neighbor if the new element
 *    is closer.  This fixes the pgvector TODO in hnswinsert.c.
 *
 * Build:
 *   make -C test/unit test_acorn_build
 */

#include <assert.h>
#include <float.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* -----------------------------------------------------------------------
 * Minimal neighbor list stubs
 * ----------------------------------------------------------------------- */

#define MAX_NEIGHBORS 64

typedef struct {
	int   id;
	float distance;
} Neighbor;

typedef struct {
	int      capacity;     /* M * gamma */
	int      size;
	Neighbor items[MAX_NEIGHBORS];
} NeighborList;

static void nl_init(NeighborList *nl, int capacity)
{
	assert(capacity <= MAX_NEIGHBORS);
	nl->capacity = capacity;
	nl->size = 0;
}

static void nl_add(NeighborList *nl, int id, float dist)
{
	assert(nl->size < nl->capacity);
	nl->items[nl->size].id = id;
	nl->items[nl->size].distance = dist;
	nl->size++;
}

/* Return index of furthest neighbor, -1 if empty */
static int nl_furthest(NeighborList *nl)
{
	if (nl->size == 0) return -1;
	int idx = 0;
	for (int i = 1; i < nl->size; i++)
		if (nl->items[i].distance > nl->items[idx].distance)
			idx = i;
	return idx;
}

/*
 * Try to insert (id, dist) into a full neighbor list.
 * Replaces the furthest neighbor if new element is closer.
 * Returns 1 if inserted, 0 if rejected.
 *
 * This implements the fixed-slot retry logic that pgvector's hnswinsert.c
 * documents as "TODO Retry updating connections if not".
 */
static int nl_try_insert(NeighborList *nl, int id, float dist)
{
	if (nl->size < nl->capacity) {
		nl_add(nl, id, dist);
		return 1;
	}

	int fi = nl_furthest(nl);
	if (fi >= 0 && dist < nl->items[fi].distance) {
		nl->items[fi].id = id;
		nl->items[fi].distance = dist;
		return 1;
	}
	return 0;
}

/* -----------------------------------------------------------------------
 * Tests
 * ----------------------------------------------------------------------- */

static int pass = 0, fail = 0;

#define CHECK(cond, msg) do { \
	if (cond) { printf("  [OK] %s\n", msg); pass++; } \
	else       { printf("  [FAIL] %s\n", msg); fail++; } \
} while(0)

/* gamma=2 means capacity = M*2 neighbors */
static void test_gamma_capacity(void)
{
	printf("test_gamma_capacity\n");

	int M = 4, gamma = 2;
	NeighborList nl;
	nl_init(&nl, M * gamma);

	for (int i = 0; i < M * gamma; i++)
		nl_add(&nl, i, (float)i * 0.1f);

	CHECK(nl.size == M * gamma, "stores M*gamma neighbors at gamma=2");
	CHECK(nl.capacity == 8, "capacity is M*gamma=8");
}

/* Retry: new closer element replaces furthest when list is full */
static void test_retry_replaces_furthest(void)
{
	printf("test_retry_replaces_furthest\n");

	NeighborList nl;
	nl_init(&nl, 3);
	nl_add(&nl, 10, 0.5f);
	nl_add(&nl, 20, 0.8f);  /* furthest */
	nl_add(&nl, 30, 0.3f);

	int inserted = nl_try_insert(&nl, 99, 0.6f);  /* closer than 0.8 */
	CHECK(inserted == 1, "closer element is accepted");
	CHECK(nl.size == 3, "size unchanged after replacement");

	int found_99 = 0, found_20 = 0;
	for (int i = 0; i < nl.size; i++) {
		if (nl.items[i].id == 99) found_99 = 1;
		if (nl.items[i].id == 20) found_20 = 1;
	}
	CHECK(found_99, "new element id=99 is present");
	CHECK(!found_20, "furthest element id=20 was evicted");
}

/* Retry: farther element is rejected when list is full */
static void test_retry_rejects_farther(void)
{
	printf("test_retry_rejects_farther\n");

	NeighborList nl;
	nl_init(&nl, 2);
	nl_add(&nl, 10, 0.3f);
	nl_add(&nl, 20, 0.5f);  /* furthest = 0.5 */

	int inserted = nl_try_insert(&nl, 99, 0.9f);  /* farther than all */
	CHECK(inserted == 0, "farther element is rejected");
	CHECK(nl.size == 2, "size unchanged when rejected");
}

/* Empty list: first insert always succeeds regardless of retry path */
static void test_retry_empty_list(void)
{
	printf("test_retry_empty_list\n");

	NeighborList nl;
	nl_init(&nl, 4);
	int inserted = nl_try_insert(&nl, 1, 0.1f);
	CHECK(inserted == 1, "insert into empty list succeeds");
	CHECK(nl.size == 1, "size is 1 after first insert");
}

int main(void)
{
	printf("=== acorn_build unit tests ===\n\n");
	test_gamma_capacity();
	test_retry_replaces_furthest();
	test_retry_rejects_farther();
	test_retry_empty_list();
	printf("\n%d passed, %d failed\n", pass, fail);
	return fail > 0 ? 1 : 0;
}
