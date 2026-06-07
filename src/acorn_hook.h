#ifndef ACORN_HOOK_H
#define ACORN_HOOK_H

/*
 * Tier 1: set_rel_pathlist_hook + CustomScan
 *
 * Detects {hnsw index + vector distance operator + WHERE clause} on a relation
 * and injects an AcornScan CustomPath with filter-aware cost estimate.
 * Falls back to normal planning when the hook is disabled or the pattern
 * is not recognized.
 */

void acorn_hook_init(void);
void acorn_hook_fini(void);

#endif /* ACORN_HOOK_H */
