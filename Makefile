.DEFAULT_GOAL := help
COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
SRC := src tests migrations evals

.PHONY: help install fmt fmt-check lint typecheck arch test test-int eval check up up-obs up-full down logs migrate seed chat prompts-push debug golden-curate golden-load

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies and git hooks
	uv sync
	uv run pre-commit install

fmt: ## Format and autofix
	uv run ruff format $(SRC)
	uv run ruff check --fix $(SRC)

fmt-check: ## Verify formatting
	uv run ruff format --check $(SRC)

lint: ## Ruff + bandit
	uv run ruff check $(SRC)
	uv run bandit -q -c pyproject.toml -r src

typecheck: ## mypy --strict
	uv run mypy

arch: ## Enforce hexagonal boundaries
	uv run lint-imports

test: ## Unit tests
	uv run pytest tests/unit -q

test-int: ## Integration tests (needs `make up`)
	uv run pytest tests/integration -q -m "integration or bq"

eval: ## Agent evals and red-team suite (real LLM)
	uv run python -m evals.run

check: fmt-check lint typecheck arch test ## Everything CI runs

up: ## Start core stack
	$(COMPOSE) --profile core up -d

up-obs: ## Start core stack + self-hosted Langfuse
	$(COMPOSE) --profile core --profile obs up -d

up-full: ## Start full stack
	$(COMPOSE) --profile core --profile obs --profile full up -d --build

down: ## Stop all
	$(COMPOSE) --profile core --profile obs --profile full down

migrate: ## Apply database migrations
	uv run alembic upgrade head

seed: ## Seed demo users, brand entitlements and golden trios
	uv run opsfleet seed
	uv run opsfleet golden load

golden-curate: ## Regenerate golden trios with the LLM, validated on BigQuery
	uv run opsfleet golden curate

golden-load: ## Embed and load the golden trios into pgvector
	uv run opsfleet golden load

chat: ## Chat as a demo user (USER_ID=mgr_a)
	uv run opsfleet chat --user $(or $(USER_ID),mgr_a)

prompts-push: ## Publish repository prompts to Langfuse (LABEL=production)
	uv run opsfleet prompts push --label $(or $(LABEL),production)

debug: ## Dump a trace (TRACE=<id>)
	uv run opsfleet debug $(TRACE)

logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=100
