.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
UV := uv
PNPM := pnpm

.PHONY: help
help: ## Show available make targets.
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*##/ {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: setup
setup: ## Install Python and Node dependencies.
	$(UV) sync --all-packages --group dev
	$(PNPM) install

.PHONY: hooks
hooks: ## Enable .githooks/pre-push.
	git config core.hooksPath .githooks

# Dev servers are per checkout: state in .argus/, ports from a hash of the path
# (scripts/dev.py). DATA_DIR= overrides the data dir; an inherited ARGUS_DATA_DIR never does.
PREVIEW_DB := $(HOME)/work/argus/data/argus.db

.PHONY: dev-api dev-dashboard seed-dev
dev-api: ## Run the API on this checkout's port with its own .argus/ data (NO_SEED=1, DATA_DIR=, ACCOUNTS=1).
	$(UV) run python scripts/dev.py api --source "$(PREVIEW_DB)" $(if $(DATA_DIR),--data-dir "$(DATA_DIR)",) $(if $(NO_SEED),--no-seed,) $(if $(ACCOUNTS),--accounts,)
dev-dashboard: ## Run the dashboard on this checkout's port, proxying its dev-api.
	python3 scripts/dev.py dashboard
seed-dev: ## Replace .argus/ data with a pruned copy of the preview DB (ACCOUNTS=1 adds accounts, PREVIEW_DB=).
	$(UV) run python scripts/dev.py seed --source "$(PREVIEW_DB)" $(if $(DATA_DIR),--data-dir "$(DATA_DIR)",) $(if $(ACCOUNTS),--accounts,)

.PHONY: image run-docker test-docker
image: ## Build the control-plane image (deploy/Dockerfile.api → argus:local).
	docker build -f deploy/Dockerfile.api -t argus:local .
run-docker: ## Start Argus via Compose (http://127.0.0.1:8000); Ctrl-C stops.
	docker compose -f deploy/compose.yaml up --build
test-docker: ## Run the real-Docker integration tests (needs a Docker daemon).
	ARGUS_DOCKER_TESTS=1 $(UV) run pytest -q -p no:cacheprovider apps/api/tests/test_docker_integration.py

.PHONY: test-k8s
test-k8s: ## Kubernetes target integration test; needs ARGUS_K8S_CONTEXT (use an isolated kind cluster).
	@test -n "$$ARGUS_K8S_CONTEXT" || { echo "set ARGUS_K8S_CONTEXT to a throwaway cluster's context (never a shared one); with kind also set ARGUS_KIND_CLUSTER"; exit 2; }
	ARGUS_DOCKER_TESTS=1 $(UV) run pytest -q -p no:cacheprovider apps/api/tests/test_docker_integration.py -k kubernetes

.PHONY: lint format typecheck test build
lint: ## Ruff + Biome + docs validation.
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) lint
	python3 scripts/validate_docs.py
	$(UV) run python scripts/check_tests.py
format: ## Auto-format Python and TypeScript.
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(PNPM) format
typecheck: ## ty + tsc.
	$(UV) run ty check
	$(PNPM) typecheck
test: ## Run Python and Node tests.
	$(UV) run pytest -n auto
	$(PNPM) test
build: ## Build the dashboard.
	$(PNPM) build

.PHONY: reload-preview
reload-preview: ## Safely fast-forward clean main, preflight migrations, back up SQLite, and restart preview API.
	@set -Eeuo pipefail; \
	root=$$(git rev-parse --show-toplevel); expected="$(HOME)/work/argus"; \
	test "$$root" = "$$expected" || { echo "Run this target from $$expected" >&2; exit 2; }; \
	test "$$(git branch --show-current)" = main || { echo "Refusing: checkout is not on main" >&2; exit 2; }; \
	test -z "$$(git status --porcelain --untracked-files=all)" || { echo "Refusing: checkout has local changes; preserve/reconcile WIP first" >&2; exit 2; }; \
	mkdir -p "$(HOME)/.cache"; exec 9>"$(HOME)/.cache/argus-preview-reload.lock"; \
	flock -n 9 || { echo "Refusing: another preview reload is running" >&2; exit 2; }; \
	for proc in /proc/[0-9]*; do \
	  cwd=$$(readlink -f "$$proc/cwd" 2>/dev/null || true); \
	  case "$$cwd" in "$$root"|"$$root"/*) ;; *) continue ;; esac; \
	  name=$$(cat "$$proc/comm" 2>/dev/null || true); \
	  case "$$name" in claude*|codex*|opencode*|argus-delegate*|cursor-agent|agent) echo "Refusing: possible active writer $$name (pid $${proc##*/}) in checkout" >&2; exit 2 ;; esac; \
	done; \
	old=$$(git rev-parse HEAD); \
	git fetch origin main; \
	git merge-base --is-ancestor "$$old" origin/main || { echo "Refusing: main diverged; fast-forward only" >&2; exit 2; }; \
	git pull --ff-only origin main; \
	new=$$(git rev-parse HEAD); \
	if [[ "$$old" != "$$new" ]] && git diff --name-only "$$old" "$$new" | grep -qE '^(apps/dashboard/|packages/.*|pnpm-lock.yaml|pnpm-workspace.yaml|package.json|turbo.json)'; then \
	  echo "Dashboard inputs changed; building dashboard"; $(MAKE) build; \
	fi; \
	.venv/bin/python -c 'from argus_api.db.migrate import load_migrations; migrations=load_migrations(); print(f"Migration preflight OK: {len(migrations)} migrations, latest={migrations[-1].version}")'; \
	db="$$root/data/argus.db"; test -f "$$db" || { echo "Refusing: service SQLite DB not found at $$db" >&2; exit 2; }; \
	backup_dir="$(HOME)/.local/state/argus/backups"; mkdir -p "$$backup_dir"; chmod 700 "$$backup_dir"; \
	backup="$$backup_dir/argus-$$(date -u +%Y%m%dT%H%M%SZ).db"; \
	python3 -c 'import sqlite3,sys; src,dst=sys.argv[1:]; a=sqlite3.connect(f"file:{src}?mode=ro",uri=True); b=sqlite3.connect(dst); a.backup(b); result=b.execute("PRAGMA quick_check").fetchone()[0]; b.close(); a.close(); assert result=="ok",result; print("Verified SQLite backup:",dst)' "$$db" "$$backup"; \
	chmod 600 "$$backup"; \
	wd=$$(systemctl --user show argus-preview.service -p WorkingDirectory --value); \
	test "$$wd" = "$$root" || { echo "Refusing: preview service points to $$wd, not $$root" >&2; exit 2; }; \
	systemctl --user restart argus-preview.service; \
	test "$$(systemctl --user show argus-preview.service -p ActiveState --value)" = active || { echo "Preview service is not active after restart" >&2; exit 1; }; \
	curl -fsS --retry 12 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/healthz | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok",d; print("Preview health:",d.get("status"))'; \
	echo "Preview reload complete at $$new (SQLite backup: $$backup). Verify authenticated task/fleet/pool reads via Argus MCP."

.PHONY: bench
bench: ## MCP inbox journey benchmark (docs/specs/mcp-operations.spec.md 3.5).
	$(UV) run python apps/api/bench/mcp_inbox.py

.PHONY: openapi openapi-check openapi-client-check
openapi: ## Regenerate openapi.json and the TS client types from the API.
	$(UV) run python scripts/export_openapi.py
	$(PNPM) --filter @argus/api-client generate
openapi-check: openapi-client-check ## Fail if openapi.json or the TS client types are stale (run `make openapi`).
	$(UV) run python scripts/export_openapi.py --check
openapi-client-check: ## Fail if schema.d.ts does not match the committed openapi.json.
	@tmp=$$(mktemp --suffix=.d.ts); trap 'rm -f "$$tmp"' EXIT; \
	$(PNPM) --filter @argus/api-client exec openapi-typescript openapi.json -o "$$tmp" --silent --default-non-nullable false; \
	cmp -s "$$tmp" packages/api-client/src/schema.d.ts || { echo "schema.d.ts is stale: run \`make openapi\`"; exit 1; }

.PHONY: release
release: ## Cut a release on main: notes, commit, tag (ARGS="--dry-run" or "--push").
	python3 scripts/release.py $(ARGS)

.PHONY: audit
audit: ## Audit dependencies for known vulnerabilities.
	$(UV) run pip-audit --skip-editable
	$(PNPM) audit --prod

.PHONY: secret-scan
secret-scan: ## Scan git history (or SECRET_SCAN_RANGE) for committed secrets.
	gitleaks git --no-banner --redact -v $(if $(SECRET_SCAN_RANGE),--log-opts="$(SECRET_SCAN_RANGE)",) .

.PHONY: check
check: lint typecheck test openapi-check ## Everything CI runs, locally.

.PHONY: check-changed
check-changed: ## Only the checks the files changed since origin/main's merge base can affect (pre-push).
	python3 scripts/check_changed.py
