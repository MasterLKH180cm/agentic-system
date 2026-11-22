# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /usr/local/bin/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENT_ENV=production \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
ARG AGENT_EXTRAS=postgres,mcp
# Reuse dependencies when only application source changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; set --; IFS=,; \
    for extra in $AGENT_EXTRAS; do set -- "$@" --extra "$extra"; done; unset IFS; \
    uv sync --locked --no-dev --no-editable --no-install-project "$@"
COPY agentic ./agentic
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; set --; IFS=,; \
    for extra in $AGENT_EXTRAS; do set -- "$@" --extra "$extra"; done; unset IFS; \
    uv sync --locked --no-dev --no-editable "$@" \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser
EXPOSE 8001
CMD ["uvicorn", "agentic.api:app", "--host", "0.0.0.0", "--port", "8001"]
