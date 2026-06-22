# pg_acorn — coding guidelines

pg_acorn is a **PostgreSQL C extension** (filterable HNSW via ACORN). Build and test with
`make` / `make install`, and `make docker-test` (pg_regress + isolation under PG17 + pgvector).
These conventions apply to all code-writing agents.

---

## Kent Beck — TDD & Tidy First

### 1. Red → Green → Refactor
- For a new behavior, add a pg_regress test (`test/sql/*.sql`) plus its golden
  (`test/expected/*.out`) — or a C unit test (`test/unit/`) — **before** implementing.
- Implement the minimum needed to make `make docker-test` pass.
- Refactor only after it is green, then re-run `make docker-test`.

### 2. Tidy First — never mix structural and behavioral changes
- Structural (rename / extract / move): behavior unchanged → golden files unchanged.
- Behavioral (new feature / fix): structure unchanged.
- If both are needed: structural commit first, behavioral commit second — separate commits.

### 3. Make it work → make it right → make it fast (prove "fast")
- Simplest thing that passes first; then remove duplication and reveal intent.
- Optimize only with evidence. This project has repeatedly measured speculative
  build/scan "optimizations" (two-pass, M-ACORN, runtime 2-hop) as no-wins — so
  **never claim a performance gain without a reproducible benchmark** (page-I/O,
  recall@QPS), and keep one source of truth for competitive numbers
  (`bench/COMPETITIVE_VERDICT.md`). Do not re-attempt the disproven hypotheses
  recorded in `docs/project-log.md`.

### 4. Commit discipline
- Commit only when `make docker-test` is green and there are no new warnings.
- One commit = one logical unit; state in the message whether it is a structural or
  behavioral change.
- Branch off `main`; end commit messages with the project's `Co-Authored-By` trailer.

### 5. Tests must be deterministic and self-validating
- pg_regress is golden-file based (pass/fail, no manual inspection); each test creates
  and drops its own schema, so tests stay independent and repeatable.
- For build/scan determinism in A/B work, set `pg_acorn.build_seed` and a serial build
  (`max_parallel_maintenance_workers = 0`) so results differ only by the change under test.
- Concurrency belongs in isolation specs (`test/specs/*.spec`), not in regress.
- Ground truth for filtered-recall A/B = exact seqscan computed **before** the index exists
  (an index-resident "truth" leaks the index into its own baseline).

### 6. Test layers (a C extension is not unit-test-heavy)
- **C unit tests** — distance kernels, page-format helpers (fast, isolated).
- **pg_regress** — the bulk: build / scan / recall / correctness against goldens.
- **isolation** — concurrent insert/scan, cache eviction safety.
- **benchmarks** (`bench/`) — performance and recall; **INDICATIVE** unless run on a quiet,
  controlled host. Not part of the correctness gate.

---

> Working if: a regress/golden or isolation test precedes the code; structural and behavioral
> changes never share a commit; "make it work" precedes "make it right"; and no performance
> claim ships without a reproducible measurement.
