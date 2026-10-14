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

.PHONY: dev-api dev-dashboard
dev-api: ## Run the API with reload on :8000.
	$(UV) run uvicorn argus_api.main:app --reload
dev-dashboard: ## Run the dashboard on :3000.
	$(PNPM) dev

.PHONY: lint format typecheck test build
lint: ## Ruff + Biome + docs validation.
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) lint
	python3 scripts/validate_docs.py
format: ## Auto-format Python and TypeScript.
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(PNPM) format
typecheck: ## ty + tsc.
	$(UV) run ty check
	$(PNPM) typecheck
test: ## Run Python and Node tests.
	$(UV) run pytest
	$(PNPM) test
build: ## Build the dashboard.
	$(PNPM) build

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

.PHONY: audit
audit: ## Audit dependencies for known vulnerabilities.
	$(UV) run pip-audit --skip-editable
	$(PNPM) audit --prod

.PHONY: secret-scan
secret-scan: ## Scan git history (or SECRET_SCAN_RANGE) for committed secrets.
	gitleaks git --no-banner --redact -v $(if $(SECRET_SCAN_RANGE),--log-opts="$(SECRET_SCAN_RANGE)",) .

.PHONY: check
check: lint typecheck test openapi-check ## Everything CI runs, locally.
