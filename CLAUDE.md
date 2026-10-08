# OpsFleet Data Analysis Agent

Design decisions: `docs/DECISIONS.md` (source of truth — update it when a decision changes).

## Architecture

- Hexagonal layout (DECISIONS §23): `api/ cli/ entities/ models/ interactors/ ports/ repos/ gateways/ agents/ core/`.
- Dependencies point inward. `interactors/` imports only `ports/` and `entities/`. Enforced by import-linter.
- Business rules (scoping, ownership, deletion confirmation) live in `interactors/`. Agent tools are thin wrappers.
- Every external system sits behind a `ports/` Protocol. Tests use fakes, not SDK mocks.
- All SQL goes through `QueryEngine`. The BigQuery client is imported only in `gateways/bigquery/`.

## Code Style

- **No comments.** Code must explain itself through naming and structure. No commented-out code, no section banners, no TODOs.
- **Docstrings: none by default.** Allowed only on public `ports/` Protocols, one line, only when the name alone is ambiguous. Never restate the signature.
- Python 3.12, `uv` for dependencies.
- Pydantic v2 at every boundary; no bare `dict` crossing layers.
- Configuration via `pydantic-settings` only — one `Settings` class in `core/config.py`, no `os.getenv` elsewhere. Invalid config fails at startup.
- Async for all I/O; blocking SDK calls (BigQuery) wrapped via `anyio.to_thread`.
- Small functions (~40 lines max), shallow nesting, early returns. Extract on second duplication.

## Errors — Fail Fast

- Validate inputs at the boundary and raise immediately; never continue with partial or guessed state.
- No `except Exception`, no bare `except`, no swallowing, no returning `None` to signal failure.
- Catch only what you can handle, only at defined boundaries:
  - agent tool boundary → typed error returned to the model for self-correction (bounded retries);
  - API/CLI boundary → mapped to error taxonomy code + user-safe message.
- Domain errors are typed (`core/errors.py`) and carry a taxonomy code (`E_SQL_SYNTAX`, `E_SCOPE_DENIED`, …).

## Tooling

- `ruff` (lint + format), `mypy --strict`, `bandit`, `import-linter`, `pytest`.
- `Makefile` is the single entry point:
  - `make install` · `make fmt` · `make lint` (ruff + bandit) · `make typecheck` · `make arch` (import-linter)
  - `make test` (unit) · `make test-int` · `make eval` · `make check` (fmt-check + lint + typecheck + arch + test)
  - `make up` / `make up-full` (compose profiles) · `make down`
- pre-commit runs `make check` subset + secret scan + denylist scan.
- Run `make check` before declaring any task done.

## Testing

- TDD for `interactors/` and the data adapter.
- `tests/unit` — fakes only, no network, < 5s total.
- `tests/integration` — real Postgres (testcontainers); BigQuery behind `-m bq` marker.
- `evals/` — real LLM; never part of `make test`.
- No real LLM calls in unit tests; use the scripted fake `LLMClient`.
- Every security rule (PII masking, brand scope, report ownership, deletion confirmation) has a red-team test.

## Security & Data

- Never log raw rows or PII; identify users by `user_hash`.
- No secrets in code or `.env.example`.
- Never commit names of previous employers, clients, or their projects (denylist pre-commit hook).
- Prior work is reference-only: never copy code from other local repositories.

## Workflow

- Small commits, one concern each, Conventional Commits.
- When a design decision changes, update `docs/DECISIONS.md` in the same change.
