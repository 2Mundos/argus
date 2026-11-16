---
status: implemented
created: 2026-11-16
updated: 2026-11-16
---

# Language validation

# 1. Purpose and Scope

All new developer prose in this repository is English: documentation, code
comments and docstrings, and commit messages. This is the rule already stated
in [AGENTS.md](../../AGENTS.md); the gate makes it enforced. It runs at three
points: locally by git hooks (`.githooks/commit-msg` for immediate feedback on
the message, `.githooks/pre-push` over the push range) and in CI by the
unconditional `language-validation` Quality job, which checks the PR's
`base..head` range. The checker is `scripts/validate_language.py`, runnable by
hand as `make language-lint` (default: worktree changes vs `HEAD`) or
`make language-lint RANGE=a..b`.

# 2. Non-Goals

- No migration of legacy text. Only added lines and new commit messages are
  checked, so history never fails and an old file can be edited without
  rewriting its non-English neighbours.
- String literals are not checked. They hold UI copy, test fixtures,
  identifiers and deliberately multilingual data (encoding probes, redaction
  examples); distinguishing developer prose from data automatically is
  unreliable, so only comments and docstrings are checked in code files. The
  English-only rule still applies to the text inside them; the gate does not
  catch a violation there.
- No new runtime or CI dependencies. Detection is stdlib-only and
  deterministic (see Decisions Made).

# 3. Decisions Made

## 3.1 Diff-scoped instead of full-tree

A full-tree gate would fail on day one given existing non-English text; scoping
to added lines plus new commit messages enforces the rule going forward with
zero legacy churn. The worktree mode is the exception: untracked files are
checked in full, because every line in them is new.

## 3.2 Heuristic detector instead of a language-model library

`langdetect` is non-deterministic and unreliable on short strings such as
commit subjects, `lingua` ships ~100MB models, and any of them would add a
dependency to jobs that currently run on plain `python3` with no venv
(docs-validation) and to every developer's hooks. The stdlib detector combines
three signals: Latin diacritics outside an English loanword and proper-noun
allowlist (`café`, `São Paulo`, `Brasília`, ...), two or more distinct
stopwords from small Portuguese/Spanish/French/German/Italian sets (curated so
common English words such as `do`, `die`, `come`, `son`, `plus`, `est` are
absent or weak; a segment needs at least one non-weak hit, so `do` plus `de`
cannot flag English), and a minimum prose length so code fragments and hashes
cannot match. Acronyms, hyphenated-compound fragments and adjacent capitalized
words are ignored so `DoS`, `de-duplicate` and `Los Angeles` pass.

## 3.3 Comments and docstrings in code, full prose in docs

Python uses the `tokenize` module (exact `COMMENT` tokens) plus AST-verified
docstrings only (module/class/function opening string statements); other string
literals -- product copy, language data -- are never checked.
TypeScript/JavaScript and shell/YAML use comment-prefix detection, plus the
text after a whitespace-preceded trailing `//`, `--` or `#` marker (the code on
the line is never checked; a bare `*` line counts only inside a `/* */` block); SQL
migrations use `--` comments; Markdown skips fenced code blocks, inline code,
URLs, refs and hashes. Conventional commit prefixes, trailers, merge commits
and `(#1234)` refs are stripped before checking messages.

## 3.4 Unconditional CI job, mirroring `secret-scan`

A violation can land in any file and commit messages never appear in path
flags, so the job is not path-gated. The pre-push hook collects ranges per pushed
ref because commit messages are invisible to the changed-file list, and runs
the language gate unconditionally next to `make check-changed`. With no usable
base (new branch, no `origin/main`) it checks the tip commit and says so rather
than skipping. Commit messages are cut at the `git commit -v` scissors line so
the appended diff is never read as prose.

# 4. Open Questions

- Should string literals be checked too? Today a UI string, log message or
  exception text in another language passes. If that becomes a real source of
  violations, add a check limited to UI copy, `logging` and exception paths,
  with an exclusion list for test fixtures and language data.

# 5. Constraints

- CI runs on plain `python3` with no venv: only the standard library may be
  used by the checker.
- Hooks must stay fast (sub-second on typical pushes): no downloads, no model
  loads; only the push range is scanned.
- Bypass paths stay consistent with the other gates: `git commit --no-verify`
  skips the commit-msg hook, `git push --no-verify` skips the pre-push gate.

# 6. Implementation Consequences

- New files: `scripts/validate_language.py` (checker), `.githooks/commit-msg`
  (new hook), this spec, and `scripts/test_validate_language.py`. No new
  dependencies, no lockfile changes.
- Modified: `.githooks/pre-push` (range check before `make check-changed`),
  `Makefile` (`language-lint` target, also run by `lint`),
  `.github/workflows/quality.yaml` (new `language-validation` job),
  `AGENTS.md` and `docs/README.md` (command and spec link).
- Developers enable hooks once with `make hooks` (`core.hooksPath` picks up the
  whole `.githooks` directory, including the new hook).

## Verification boundary

- Commands: `python3 scripts/validate_language.py`,
  `uv run pytest scripts/test_validate_language.py`, `make lint`.
- Verified: unit tests cover language detection probes (English loanwords,
  proper nouns, acronyms, compounds, Latin phrases, Portuguese and diacritic
  evidence), added-line parsing, comment/docstring extraction and commit-message
  cleaning; the script itself runs in `make lint` and the CI job.
- Not verified: a real push through the installed hook on every developer's
  machine; the detector's recall on arbitrary non-Portuguese prose beyond the
  small stopword sets.
