---
status: implemented
created: 2026-11-05
updated: 2026-11-05
---

# Changed-only checks

# 1. Purpose and Scope

The pre-push hook used to run `make check` and `make build` on the whole
repository, so a one-line docs or dashboard change ran the full Python suite, ty,
tsc and the OpenAPI check, and long hooks dropped pushes. `make check-changed`
(`scripts/check_changed.py`) runs only the checks the changed files can affect,
and `.githooks/pre-push` runs it. Promoted from the 2026-10-31 changed-only-checks
draft.

# 2. Non-Goals

- No change to what CI runs: CI keeps the full suite, and `make check` stays the
  full local run.
- No skipping of the secret scan: the hook runs `make secret-scan` on every push.

# 3. Decisions Made

## 3.1 What changed

The changed set is the working tree (committed and uncommitted changes, untracked
files, deletions) against the merge base with `origin/main`. When the merge base
cannot be found, the full `make check` and `make build` run.

## 3.2 What runs

| Changed | Runs |
| --- | --- |
| a lock file, `pyproject.toml` or `package.json` | only `make check` and `make build` |
| a migration (`apps/api/src/argus_api/db/migrations/*.sql`) | full pytest suite and the migration preflight |
| other `*.py`, a test, or a shell file covered by a Python test (`deploy/argus`, `deploy/install.sh`) | pytest on the tests beside the changed modules and the tests that import them (a static import graph); the full suite when the graph cannot decide |
| any `*.py` | ruff check and ruff format on the changed files |
| `*.py` under `apps/api/src` or `packages/core/src` | `ty check` (whole project: it needs cross-package types) |
| an API router or model, or the OpenAPI files | `make openapi-check` |
| Biome-covered files (`.ts`, `.tsx`, `.js`, `.json`, `.css`, …) | Biome on the changed files |
| `apps/dashboard/**`, `packages/api-client/**` | tsc, Vitest on related tests, the dashboard build |
| `docs/**` | `validate_docs.py`; `check_specs.py` too for `docs/specs/`, `docs/drafts/` or the script |

## 3.3 How it runs

- Steps run concurrently while their CPU weights fit a budget (`ARGUS_CHECK_JOBS`,
  default the CPU count); each prints its output when it finishes, and a step past
  its timeout is killed and reported as `TIMEOUT`.
- A passing run records a fingerprint of the merge base and the changed files'
  contents in the git dir (`argus-checked-changes`); a rerun with the same
  fingerprint prints the earlier summary and exits.
- `--plan` prints the steps without running them.
- The hook refuses to run unless the working tree is clean and at the pushed
  commit, and unsets git's hook environment before running checks.

# 4. Open Questions

- Whether the import graph should also follow dynamic imports and fixtures; today
  an undecidable case falls back to the full suite.

# 5. Constraints

- Standard library only; it must run before `uv sync` has been done.
- A docs-only push finishes in seconds; a dashboard-only push never runs pytest.

# 6. Implementation Consequences

- `scripts/check_changed.py`, `make check-changed`, `.githooks/pre-push`.
- Tests: `scripts/test_check_changed.py`, `scripts/test_pre_push.py`.

## Verification boundary

- Commands: `uv run pytest scripts/test_check_changed.py scripts/test_pre_push.py`;
  `python3 scripts/check_changed.py --plan`.
- Verified: the plan for each kind of change, the graph, the pass record and the
  hook's clean-tree refusal, at unit level; used on every push since 2026-10-31.
- Not verified: CPU budget behaviour under a loaded machine beyond daily use.
