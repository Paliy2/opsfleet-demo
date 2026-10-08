FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM deps AS build
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
ARG SITE=/app/.venv/lib/python3.12/site-packages
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
WORKDIR /app
RUN chown app:app /app
COPY --from=deps --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app ${SITE}/opsfleet_agent ${SITE}/opsfleet_agent
COPY --from=build --chown=app:app ${SITE}/opsfleet_agent-0.1.0.dist-info ${SITE}/opsfleet_agent-0.1.0.dist-info
COPY --from=build --chown=app:app /app/.venv/bin/opsfleet /app/.venv/bin/opsfleet-api /app/.venv/bin/opsfleet-worker /app/.venv/bin/
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app data/golden ./data/golden
COPY --chown=app:app evals ./evals
COPY --chown=app:app src/opsfleet_agent/ui ./src/opsfleet_agent/ui
COPY --chown=app:app .chainlit ./.chainlit
COPY --chown=app:app chainlit.md ./
COPY --chown=app:app public ./public
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    OPSFLEET_API_HOST=0.0.0.0 \
    OPSFLEET_ARTIFACTS_DIR=/tmp/artifacts
USER 10001
EXPOSE 8000 9464
CMD ["opsfleet-api"]
