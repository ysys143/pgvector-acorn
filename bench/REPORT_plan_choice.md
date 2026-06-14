# P2 — plan-choice diagnostic: is the acorn cost model routing correctly?

Date: 2026-06-14. Harness: `bench/plan_choice_probe.py`. Results:
`bench/results_plan_choice_probe.json`. 250K correlated fixture, `tv_acorn_noinline`
(gamma=2) + `tv_bucket_btree` (so the bitmap PREFILTER path exists). ef=100.

## Question

`acorn_cost.c` chooses between the acorn KNN index scan and a bitmap PREFILTER
(Bitmap Heap Scan on the filter btree + Sort by distance, exact / recall 1.0)
on cost alone, with "no recall signal" (a documented limitation). P2 asks:
does that ever mis-route — pick acorn where the exact prefilter is genuinely
faster (the low-cardinality case), or vice versa?

## Method

Across sel 1/2/5/10/20/50% (bucket < k ≈ k%), for the filtered top-10 query,
record (a) the planner's default pick, (b) the estimated cost AND EXPLAIN ANALYZE
time of acorn-forced vs prefilter-forced plans. Run twice.

## Result

Estimated cost is deterministic (pure function of stats) and IDENTICAL across
both runs; EXPLAIN ANALYZE times were dominated by host contention (a co-tenant
container), so treat cost as the reliable signal and times as indicative.

| sel | planner pick | acorn cost | prefilter cost | prefilter/acorn | acorn ms (clean) | prefilter ms (sel>=5%) |
|----:|------|---:|---:|---:|---:|---:|
| 1%  | acorn | 236.5 |  6546.9 |  28x | ~3 | 6 |
| 2%  | acorn | 118.1 | 11162.5 |  94x | ~5 | 53 |
| 5%  | acorn |  84.0 | 17870.9 | 213x | ~3 | 118-485 |
| 10% | acorn |  74.4 | 20064.9 | 270x | ~3 | 233-432 |
| 20% | acorn |  64.0 | 20406.4 | 319x | ~4-6 | 322-325 |
| 50% | acorn |  55.4 | 24138.7 | 436x | ~3 | 929-1047 |

- The planner picks **acorn across the entire 1-50% range**, and acorn's cost is
  **28-436x lower** than the prefilter's — correctly reflecting that the prefilter
  sorts thousands of passing rows while acorn expands an ef-bounded frontier.
- acorn cost DECREASES with selectivity (n_expand = 40/sel shrinks); prefilter
  cost INCREASES (more rows to sort). They do not cross in 1-50%.
- The two apparent time "mismatches" landed at DIFFERENT selectivities across the
  two runs (sel 2% in run 1, sel 1% in run 2) — i.e. host noise, not a stable
  cost-model error. When uncontended, acorn is ~3-6 ms while the prefilter is
  100-1000 ms at sel >= 5%.

## Tiny-cardinality extreme

The cost model already routes the extreme correctly: `n_expand = Min(40/sel, N)`
saturates to N as sel -> 0, making acorn's cost approach a full scan, so the
planner switches to the exact prefilter below ~0.016% selectivity (40/N rows).
That is below this fixture's bucket granularity (96 values -> 1% minimum), so it
is not directly measurable here, but it is the built-in equivalent of Qdrant's
small-cardinality exact branch.

## Verdict — no cost-model change

The current selectivity-aware model routes correctly where it is measurable
(1-50%: acorn, by a 28-436x cost margin and a large time margin) and handles the
tiny-cardinality extreme via n_expand saturation. P2's hypothesized mis-routing
does NOT reproduce. The only residual gap is a sub-1% band (both plans < ~5 ms,
negligible absolute difference) that is below the fixture granularity and whose
fix (ef-aware cost) is exactly the change that previously destabilized mid-band
plan choice (see acorn_cost.c). Changing the model would add that regression risk
for no demonstrated benefit, so the evidence-based decision is to leave it
unchanged and record this investigation. The probe stays as a regression guard:
re-run after any future cost change to confirm acorn still wins 1-50%.

## Caveats

Single correlated fixture, 250K, one loaded dev host (times noisy; costs
deterministic). Selectivity floor 1% (bucket granularity) — the sub-1% crossover
is inferred from the cost formula, not measured.
