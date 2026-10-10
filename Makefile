.DEFAULT_GOAL := help
export GRPC_VERBOSITY ?= ERROR
COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
SRC := src tests migrations evals
OBS_ENV := LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_ENCRYPTION_KEY LANGFUSE_ADMIN_PASSWORD \
	OPSFLEET_LANGFUSE_HOST OPSFLEET_LANGFUSE_PUBLIC_KEY OPSFLEET_LANGFUSE_SECRET_KEY
FULL_ENV := $(OBS_ENV) KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_DEMO_PASSWORD CHAINLIT_AUTH_SECRET OPSFLEET_PII_VALUE_KEY
require-env = @for name in $(1); do grep -Eq "^$$name=.+" .env || { echo "set $$name in .env"; exit 1; }; done

.PHONY: help requirements install fmt fmt-check lint typecheck arch test test-int eval check up up-obs up-full down logs migrate seed chat prompts-push debug golden-curate golden-load golden-ablation rag-eval pii-dataset pii-eval

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

golden-ablation: ## A/B: agent with vs without golden examples (real LLM; ARGS="--repeats 2")
	uv run python -m evals.ablation $(ARGS)

rag-eval: ## Golden retrieval quality: hit@k, MRR, nDCG, abstention (embeddings only; ARGS="--mode labelled")
	uv run python -m evals.retrieval $(ARGS)

check: fmt-check lint typecheck arch test ## Everything CI runs

requirements: ## Regenerate requirements.txt from uv.lock (pip users)
	uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt -q

up: ## Start core stack
	$(COMPOSE) --profile core up -d

up-obs: ## Start core stack + self-hosted Langfuse
	$(call require-env,$(OBS_ENV))
	$(COMPOSE) --profile core --profile obs up -d

up-full: ## Start full stack
	$(call require-env,$(FULL_ENV))
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

pii-dataset: ## Regenerate the PII benchmark dataset (deterministic)
	uv run python -m evals.pii.generate

pii-eval: ## PII redaction benchmark, 500 cases (REGEX_ONLY=1 skips DLP)
	uv run python -m evals.pii.run $(if $(REGEX_ONLY),--regex-only)
