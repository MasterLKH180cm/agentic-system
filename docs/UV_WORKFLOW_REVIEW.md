# uv workflow implementation review

Local verification date: 2026-11-22. Scope: the
[uv workflow specification](UV_WORKFLOW_SPEC.md), dependency installation,
deployment documentation and explicit database selection.

## Changes

- Added the generated `uv.lock` (70 resolved packages, including optional extras)
  and `.python-version` (3.12); `requires-python = ">=3.11"` remains unchanged.
- CI uses uv 0.13.9 and a pinned setup-uv action. Job-specific cache suffixes,
  including the research engine matrix value, prevent unrelated dependency sets
  from competing for the same immutable cache key. Model caches remain separate.
- Agent dependencies are synced from its lock. The companion is installed from
  its exact checkout using exported RAG requirements and Agent constraints;
  `uv pip check` confirms compatibility. Neither `--no-deps` nor an implicit
  post-install exact sync hides or removes companion dependencies.
- Docker installs locked dependencies before copying application source, uses
  BuildKit's uv cache, preserves configurable extras and UID 10001, and runs the
  installed interpreter directly. Application startup does not resolve packages.
- Invalid nonempty `AGENT_DATABASE_URL` schemes now fail before Store creation.
  Both PostgreSQL URI aliases remain supported, take precedence over the local
  path, and never fall back to SQLite on connection failure. An unset/empty URL
  preserves explicit `AGENT_DB_PATH` compatibility. Errors omit DSN credentials.
- README explains uv setup and extras, the two-service test environment, storage
  selection, container-only configuration paths, capability/health limits and
  the write/restart side effects of the existing full-stack smoke command.
- MCP client examples select this checkout's prepared uv environment by absolute
  directory; the missing-dependency hint uses the same installation workflow.

## Local results

All tests ran in a new repository-local environment installed with uv, without
changing the previous shared environment. Telemetry was disabled.

| Check | Result |
| --- | --- |
| `uv lock --check` | Passed; lock matches project metadata |
| Locked sync and two-repository constrained installation | Passed |
| `uv pip check` | Passed |
| Unit suite | 170 passed, 249 deselected, no skips/errors |
| Ordinary integration suite | 244 passed, 175 deselected, no skips/errors |
| Cumulative complete-package statement coverage | 2,644 / 2,925 = 90.3932% |
| Cumulative complete-package branch coverage | 960 / 1,158 = 82.9016% |
| Existing 85% statement / 75% branch floors | Passed, unchanged |
| Existing Ruff checks and `git diff --check` | Passed |
| Workflow YAML and 39 Bash run-step syntax checks | Passed |
| v0.2 real HTTP, independent worker and stdio MCP | All 7 reported groups passed |
| v0.3 real evidence/refusal, REST/SSE and both MCP bridges | All 3 reported groups passed |
| SQLite worker-loss/retry/cancellation/SSE/deadline resilience | All 6 scenarios passed; cleanup completed |

The new configuration tests include six invalid scheme/path cases, two valid
PostgreSQL aliases, and two unset/empty URL cases. Their fixture clears unrelated
Agent/provider settings for the test and restores the caller's environment.

The first ordinary integration attempt encountered 50 failures because this
workspace supplies a SOCKS proxy and the fresh environment did not include
HTTPX's optional `socksio`. Installing the same locked `httpx==0.28.1` with its
`socks` extra in the local test environment resolved that platform prerequisite;
proxy settings and product dependencies were not changed. The first failure
JUnit was retained under ignored `reports/testing/initial-proxy-failure/`.
Coverage was reset with a fresh unit run before the successful integration run.
CI continues to install the locked product/test extras without this local-only
addition. Starlette emits one existing HTTPX TestClient deprecation warning;
the selected tests pass.

## Installation timing boundary

A disposable environment was populated from an already warm cache in 0.2188 s.
Three repeat locked no-op syncs took 0.0548, 0.0131 and 0.0140 s (median 0.0140 s)
using Python 3.12.14 and uv 0.13.9. This selected `dev,mcp,postgres,storage,telemetry,e2e`
extras and did not install or execute a trained model. Raw observations are in
ignored `reports/testing/uv-sync-timing.json`.

These are local installation observations, not a cold-cache benchmark, a
pip-versus-uv speedup ratio, or improvements to API latency/model inference.

## Resource and review boundaries

The three local E2E harnesses used actual HTTP servers, independent workers,
stdio MCP and temporary SQLite stores. This environment has no Docker or
PostgreSQL server; it did not run the changed Docker image, native pgvector,
Redis/S3, Chromium, real research engines or the frozen admission model locally.
Their existing CI jobs remain required evidence for the exact PR revision.

The companion checkout in every CI job must point to the reviewed RAG commit
that contains its lock. No workflow-state, approval, accounting, lease-fencing,
retry, cancellation, model-quality or coverage-policy changes are included.
