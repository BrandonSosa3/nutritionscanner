.DEFAULT_GOAL := help
SHELL := /bin/bash
BACKEND := backend
VENV := $(BACKEND)/.venv/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS=":.*?## "}; {printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'

# ── Environment ───────────────────────────────────────────────────────────

.PHONY: setup
setup: ## Cold-start: create .env, build images, install deps, migrate
	@test -f .env || (cp .env.example .env && echo "Created .env — add your API keys")
	$(MAKE) install
	$(MAKE) up
	$(MAKE) migrate
	@echo ""
	@echo "Ready. API: http://localhost:8000/health  Docs: http://localhost:8000/docs"

.PHONY: install
install: ## Install backend dependencies from uv.lock into backend/.venv
	cd $(BACKEND) && uv sync --frozen --extra dev

.PHONY: lock
lock: ## Re-resolve dependencies and update uv.lock (after editing pyproject)
	cd $(BACKEND) && uv lock

# ── Stack ─────────────────────────────────────────────────────────────────

.PHONY: up
up: ## Start the stack (postgres, redis, api, worker)
	docker compose up -d --build
	@docker compose ps --format "table {{.Service}}\t{{.Status}}"

.PHONY: down
down: ## Stop the stack, keeping data volumes
	docker compose down

.PHONY: reset
reset: ## Stop the stack and DELETE all local data
	docker compose down -v

.PHONY: logs
logs: ## Tail logs from all services
	docker compose logs -f

.PHONY: shell
shell: ## Open a shell in the api container
	docker compose exec api bash

.PHONY: psql
psql: ## Open psql against the local database
	docker compose exec postgres psql -U ns -d nutritionscanner

# ── Migrations ────────────────────────────────────────────────────────────

.PHONY: migrate
migrate: ## Apply all migrations
	docker compose exec -T api alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add receipts"
	@test -n "$(m)" || (echo "Usage: make migration m=\"description\"" && exit 1)
	docker compose exec -T api alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	docker compose exec -T api alembic downgrade -1

.PHONY: history
history: ## Show migration history
	docker compose exec -T api alembic history --verbose

# ── Quality ───────────────────────────────────────────────────────────────

.PHONY: check
check: lint types test ## Run everything CI runs

.PHONY: lint
lint: ## Lint and format check
	cd $(BACKEND) && .venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/

.PHONY: fmt
fmt: ## Auto-fix lint and format
	cd $(BACKEND) && .venv/bin/ruff check --fix src/ tests/ && .venv/bin/ruff format src/ tests/

.PHONY: types
types: ## Type check (mypy strict)
	cd $(BACKEND) && .venv/bin/mypy src/

.PHONY: test
test: ## Run all tests (needs the stack up for integration tests)
	cd $(BACKEND) && .venv/bin/pytest

.PHONY: test-unit
test-unit: ## Run unit tests only (no services needed)
	cd $(BACKEND) && .venv/bin/pytest -m "not integration and not llm"

.PHONY: cov
cov: ## Test with coverage report
	cd $(BACKEND) && .venv/bin/pytest --cov --cov-report=term-missing

# ── Data ──────────────────────────────────────────────────────────────────

.PHONY: backup
backup: ## Dump the local database to backups/
	@mkdir -p backups
	docker compose exec -T postgres pg_dump -U ns -d nutritionscanner \
		| gzip > backups/ns_$$(date -u +%Y%m%dT%H%M%SZ).sql.gz
	@ls -lh backups/ | tail -1
