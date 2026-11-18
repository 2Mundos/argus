---
status: implemented
created: 2026-11-18
updated: 2026-11-18
---

# Path and type gate

# 1. Purpose and Scope

A small model once committed `pr-body.txt` at the repository root of a docs-only
PR, and earlier tasks left `.argus-deliver-body.md` in a worktree. No gate looked at
which files a change adds or where they live. `scripts/check_paths.py` is a
deterministic gate that rejects new files that make no sense for their type and
location, and says what to do about each. It runs in `make check-changed` (so the
pre-push hook), in `argus-task deliver` before it pushes, and in CI, so a bypassed
hook is still caught server-side.

# 2. Non-Goals

- No judgement of file contents, only of path, type, size and binary-ness.
- No retroactive cleanup: files already in the tree are never judged by a PR.
- No per-file override comments: the policy file is the only override.

# 3. Decisions Made

## 3.1 What is judged

The paths added or renamed-to (`git diff --name-status --diff-filter=AR`, a rename at
its new path) between the merge base with `origin/main` and HEAD. `--base REF` picks
another base (CI passes the PR base sha). `--all` judges every file at HEAD: it is
the baseline proof in CI, in `make lint` and in tests. A missing merge base is an
error (exit 2), never a pass.

## 3.2 Rules

The rules are data in `scripts/path_policy.json`; every message ends with the fix.

| Rule id | Fails when |
| --- | --- |
| `root-file` | a new file directly at the root is not in `root_files` |
| `deny-pattern` | the name matches `deny_patterns` (`pr-body*`, `*-body.md`, `.argus-*`, `*.log`, `*.tmp`, `*.bak`, `*.orig`, `*.rej`, `*.swp`, `*~`, `nohup.out`, `.DS_Store`, `core.[0-9]*`, `scratch*`, `notes*.txt`, `output*.txt`) or the path contains a `deny_dirs` directory (`__pycache__/`, `node_modules/`, `.venv/`, `.pytest_cache/`); `deny_exceptions` lists tracked fixtures that match |
| `extension` | the file type is not in `extensions` for the longest matching directory prefix (an entry starting with `.` is a suffix, any other is an exact file name) |
| `unknown-dir` | no `extensions` prefix matches: a new top-level directory |
| `name-convention` | a path under a `names` prefix does not match its pattern: `docs/runbooks/YYYY-MM-DD.<slug>.ref.md`, `docs/specs/<slug>.spec.md`, drafts, research, reports, postmortems and releases as in `docs/README.md` |
| `size` | a new file is over `max_bytes` (512 KB) and not in `large_files` |
| `binary` | a new binary file is outside `binary_dirs` |

## 3.3 Output and overrides

One line per violation, `path: rule-id: message`, exit 1; nothing is printed when
the change is clean. `--json` prints a list of `{path, rule, action}`. When
`scripts/path_policy.json` is part of the change, the text output ends with a
`policy changed` notice so reviewers see it. Editing the policy in the same PR is the
only way to add an unusual file.

## 3.4 Where it runs

- `scripts/check_changed.py` runs it first, before the pass record is consulted,
  because it judges commits rather than the file contents the record fingerprints.
  With no merge base it runs `--all`.
- `argus-task deliver` runs `python3 scripts/check_paths.py --json` as the first
  pre-push check; a failure returns the violations and joins their `action` texts into
  the error's `action`.
- CI job `path-gate`: the PR check against the base sha, and `--all` on every run.
- Models are told in `AGENTS.md` and the worker prompt's delivery-path sentence
  (`integrations/nanoclaw/argus.ts`): scratch text, PR bodies and logs go under
  `/tmp`; commit only files the task is about.

# 4. Open Questions

- Whether untracked files should also be reported by a local `make check-changed`
  (today only commits are judged; the hook and deliver require a clean tree).

# 5. Constraints

- Standard library only; it must run before `uv sync`.
- The extension table is derived from the tree at introduction and stays explicit.

# 6. Implementation Consequences

- `scripts/check_paths.py`, `scripts/path_policy.json`, `scripts/check_changed.py`,
  `Makefile` (`lint`), `.github/workflows/quality.yaml`,
  `integrations/nanoclaw/argus-task.mjs`, `integrations/nanoclaw/argus.ts`.
- Tests: `scripts/test_check_paths.py`, `scripts/test_check_changed.py`,
  `integrations/nanoclaw/argus-task.test.mjs`.

## Verification boundary

- Commands: `uv run pytest scripts/test_check_paths.py scripts/test_check_changed.py`;
  `node --test integrations/nanoclaw/argus-task.test.mjs`;
  `python3 scripts/check_paths.py --all`.
- Verified: each rule, rename and existing-file handling, the policy notice and the
  JSON shape on temporary git repos; the real tree at HEAD has zero violations;
  deliver passes a violation's action through (fake `python3`).
- Not verified: the CI job on a real pull request.
