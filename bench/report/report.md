# pg_acorn Benchmark Report

## Scenario A: Filter Selectivity Sweep

Target                        1%      5%     10%     40%     80%
----------------------------------------------------------------
pgvector                  1.000  1.000  1.000  0.976  0.966
pg_acorn_tier1_g1         1.000  1.000  1.000  1.000  1.000
pg_acorn_tier2_g1         0.996  0.960  0.888  0.914  0.908
pg_acorn_tier2_g2         1.000  0.992  0.988  0.984  0.986


## Scenario A: Page Accesses per Query (shared hit + read)

Target                         1%       5%      10%      40%      80%
---------------------------------------------------------------------
pgvector                    27359    12747     7872     1251     1237
pg_acorn_tier1_g1           27410    12733     7901     2508     1393
pg_acorn_tier2_g1           19642     8536     5295     1758      974
pg_acorn_tier2_g2           21847    12652     8581     3018     1698


## Scenario B: Post-Filter Recall (pgvector CTE workaround)

(not run)