.DEFAULT_GOAL := help
COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
SRC := src tests

.PHONY: help install fmt fmt-check lint typecheck arch test test-int eval check up up-full down logs

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

up-full: ## Start full stack
	$(COMPOSE) --profile core --profile full up -d

down: ## Stop all
	$(COMPOSE) --profile core --profile full down

logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=100
