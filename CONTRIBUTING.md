# Contributing to Argus

Thanks for helping. This page is the short version; [`AGENTS.md`](AGENTS.md) is
the full set of rules, and it applies to people as much as to AI agents.

## Before you start

- **Spec first.** A behaviour change starts as a spec in
  [`docs/specs/`](docs/specs/) (`topic.spec.md`, six-section template in
  [`docs/templates/`](docs/templates/)) or an edit to the spec it changes. Other
  docs are named `YYYY-MM-DD.name.type.md`, and every doc is linked from
  [`docs/README.md`](docs/README.md). Living specs are rewritten, not appended to.
- **Single-node first.** Argus is one process with one SQLite database and no
  required external service ([single-node](docs/specs/single-node.spec.md)).
  Proposals that add a required Postgres, Redis, broker or similar need a spec
  PR first.
- **Small is good.** Code is a liability; add only what the change needs.
- For a larger change, open a feature request first so the direction can be
  agreed before you write code.

## Setup

The toolchain is pinned in [`mise.toml`](mise.toml) (`mise install`).

```bash
make setup          # uv sync + pnpm install
make hooks          # enable the pre-push hook
```

The pre-push hook runs `make check-changed` (the checks the changed files can
affect) and the gitleaks secret scan. CI still runs everything.

## Making a change

```bash
make format         # Ruff (Python) and Biome (TypeScript) fix formatting
make check          # lint + typecheck + test, what CI runs
make openapi        # after changing API models, regenerate the contract and client
```

- Schema changes are a new numbered migration in
  `apps/api/src/argus_api/db/migrations/`; never edit one that has shipped.
- A bug fix comes with a test that fails without it. See the Tests section of
  [`AGENTS.md`](AGENTS.md) for what is worth testing and what is not.
- Check every surface the change touches (dashboard, REST API, MCP tools,
  engines, docs), as listed in [`AGENTS.md`](AGENTS.md).

## Commits and pull requests

- **English only**: code, comments, docs, commits, pull requests, reviews and
  issues.
- Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org)
  (`feat(scope): …`, `fix: …`, `docs: …`). Release notes and the next version
  are built from them ([releases](docs/specs/releases.spec.md)); mark a
  breaking change with `!` or a `BREAKING CHANGE:` footer.
- Fill in the pull request template: what and why, the spec it implements, and
  the checks you ran. A visual change includes before/after screenshots.
- Never commit secrets.

## Reporting

- Bugs and feature requests: [open an issue](https://github.com/2Mundos/argus/issues/new/choose).
- Security problems: report privately, see [`SECURITY.md`](SECURITY.md).

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
