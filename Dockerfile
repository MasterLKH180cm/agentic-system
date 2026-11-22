FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENT_ENV=production
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentic ./agentic
ARG AGENT_EXTRAS=postgres,mcp
RUN pip install --no-cache-dir ".[${AGENT_EXTRAS}]" \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser
EXPOSE 8001
CMD ["uvicorn", "agentic.api:app", "--host", "0.0.0.0", "--port", "8001"]
