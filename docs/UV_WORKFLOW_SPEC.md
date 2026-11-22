# uv workflow and explicit database selection

Status: implementation specification, 2026-11-22.

## Goal

Use uv for installation, development, CI and Docker while retaining durable
workflow semantics and the separate RAG knowledge service.

## Required behavior

- Commit a generated `uv.lock` and Python 3.12 development pin. Preserve the
  declared supported Python range where dependencies permit it.
- Use uv 0.13.9 and verified setup-uv commit
  `68559e99cc807c1eb7e9238fd43a15bba460eec2` (v10.3.3), with dependency caching.
  Install the Agent from its lock with explicit extras. Existing two-service
  tests must continue to use the exact checked-out companion RAG revision.
- Preserve extras added for companion-service tests when running commands;
  avoid an implicit exact sync that removes the companion or its dependencies.
  Keep test coverage gates, independent-worker tests, browser tests, real
  external storage, research-chain and frozen-admission jobs unchanged in scope.
- Docker uses uv with cached dependency layers, configurable extras, an
  unprivileged runtime user and direct installed commands at application startup.
- Update active README install/test/start instructions, uv installation guidance,
  lock maintenance and optional-extra behavior. Keep historical reports intact.
- Explain storage defaults: Compose stores workflow state in PostgreSQL and
  knowledge in the RAG service's separate PostgreSQL/pgvector database. Agent
  memory is bounded lexical retrieval, not an additional vector database.
- A nonempty `AGENT_DATABASE_URL` must use `postgresql://` or `postgres://`.
  Reject unsupported/typo schemes before the Store can interpret them as SQLite
  file paths. Preserve explicit `AGENT_DB_PATH` local-development compatibility.
  Failures of a selected PostgreSQL database must never select SQLite silently.
- Document authenticated storage health and the existing full-stack smoke command.

## Validation

Check the generated lock and sync a new environment. Add focused regression
tests for invalid/valid database URL selection; run existing unit/integration
tests, lint and the real two-service smoke. Use existing PostgreSQL/Redis/S3,
Compose, browser, research-chain and model-runtime CI for their actual resources.
Observe a warm repeat sync without describing it as runtime inference or SQL
acceleration. No new coverage thresholds or quality acceptance gates are added.

## Scope limits

No changes to workflow transactions, lease fencing, approvals, retry/cancellation
semantics or accounting. PostgreSQL advisory-lock contention and production
capacity require a separate workload measurement before optimization.
