# pg_acorn Benchmark Report

## Scenario A: Filter Selectivity Sweep

Target                        1%      5%     10%     40%     80%
----------------------------------------------------------------
pgvector                  1.000  1.000  1.000  0.982  0.978
pg_acorn_tier1_g1         1.000  1.000  1.000  1.000  1.000
pg_acorn_tier2_g1         0.998  0.952  0.884  0.932  0.942
pg_acorn_tier2_g2         1.000  0.992  0.988  0.970  0.966


## Scenario A: Page Accesses per Query (shared hit + read)

Target                         1%       5%      10%      40%      80%
---------------------------------------------------------------------
pgvector                    27341    12801     7957     1250     1236
pg_acorn_tier1_g1           27360    12700     7897     2528     1410
pg_acorn_tier2_g1           22001    11083     7667     2386     2373
pg_acorn_tier2_g2             371    16837    12781     4210     4196


## Scenario B: Post-Filter Recall (pgvector CTE workaround)

(not run)