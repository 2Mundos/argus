---
status: implemented
created: 2026-11-05
updated: 2026-11-05
---

# Test hygiene checks

# 1. Purpose and Scope

Tests that wait on wall time are slow and flaky under `pytest -n auto`, and skips
or lint suppressions used to land without a reason. `scripts/check_tests.py`
checks the test rules in AGENTS.md. It runs strict (`--strict`) in `make lint` and
in CI for Python and for the TS/JS tests. Promoted from the 2026-10-31
test-hygiene-lint draft.

# 2. Non-Goals

- Not a ruff plugin or a pytest plugin: a standalone script.
- Real-process integration scripts (`scripts/*-e2e.sh`) are not scanned.
- Fake scripts written as strings inside tests are not test code and are not scanned.

# 3. Decisions Made

## 3.1 Python rules (AST)

Scanned: `apps/api`, `packages/core`, `scripts`. Each hit names its rule:

| Rule | Hit |
| --- | --- |
| `sleep` | `time.sleep` / `asyncio.sleep` with a non-zero argument in test code |
| `poll-loop` | a loop that checks a condition and sleeps between checks |
| `elapsed-time` | assertions on `time.time()`, `time.monotonic()`, `perf_counter()` or `datetime.now()` deltas |
| `timeout-retry` | per-test timeout or retry/flaky/rerun markers |
| `skip-reason` | `skip` / `skipif` / `xfail` without `reason=` |
| `suppress-reason` | `# noqa`, `# type: ignore` or `biome-ignore` without a written reason |
| `tx-clock` | in application code, a clock read inside a function that receives a `Tx` (use `tx.now`) |

## 3.2 TS/JS rules (line scan)

Scanned: `*.test.ts`, `*.test.tsx`, `*.test.mjs` under `integrations/nanoclaw` and
`apps/dashboard/src`: fixed-delay `setTimeout(resolve, N)` waits (a zero delay is
fine) and `timeout:` / `retry:` options. `argus-test-until.ts` is the one
sanctioned wait helper.

## 3.3 Exceptions

One comment on the line above names the rule and the reason:
`# check-tests: allow sleep — fake CLI must outlive the poll` (`//` in TS). A
directive with no reason suppresses nothing. Without `--strict` the script only
reports.

# 4. Open Questions

- Whether `tx-clock` should cover every module under `argus_api`, not only
  functions that receive a `Tx`.

# 5. Constraints

- Standard library only (`ast`, `tokenize`); no parser for TS/JS.
- Must stay fast enough for `make lint` and pre-push.

# 6. Implementation Consequences

- `scripts/check_tests.py`; `make lint` and `.github/workflows/quality.yaml` run it with `--strict`.
- Tests: `scripts/test_check_tests.py`.

## Verification boundary

- Commands: `uv run python scripts/check_tests.py --strict`; `uv run pytest scripts/test_check_tests.py`.
- Verified: each rule and the allow directive at unit level; the repo has 0 hits on main.
- Not verified: hits in dynamically built test code.
