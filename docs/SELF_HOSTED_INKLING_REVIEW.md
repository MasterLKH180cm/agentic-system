# Self-hosted Inkling integration review

## Result and scope

The `inkling-local` route preset supports an operator-run OpenAI-compatible
server without requiring a Tinker account or a dummy API key. The existing
hosted preset, provider selector and default four-service Compose deployment
retain their behavior. This change does not download model weights or embed an
inference runtime into the Agent API/worker.

The implementation follows [SELF_HOSTED_INKLING_SPEC.md](SELF_HOSTED_INKLING_SPEC.md).
Use the RAGs [pinned local-serving guide](https://github.com/MasterLKH180cm/RAGs/blob/master/docs/INKLING_LOCAL.md)
for the model artifact, runtime revision, resource preparation and benchmark
commands. The verification below uses explicit local HTTP fixtures and offline
model behavior; it establishes integration correctness, not real Inkling speed
or quality. The original v0.5 hosted missing-credential and storage benchmark
records remain historical evidence.

## Configuration and transport decisions

- Route authentication defaults to `required`. Explicit `none` accepts only
  clean literal IPv4 loopback or `::1` endpoints. DNS names, LAN/public addresses,
  URL credentials and scoped/mapped IPv6 forms are rejected.
- Unauthenticated routes omit Authorization, ignore environment proxies and
  cannot configure `api_key_env`. Credential loading is lazy per selected route,
  so ambient provider secret values/files are never loaded for a local no-auth
  deployment. Authenticated local presets require a dedicated explicit key name.
- The local preset selects `max_tokens` and leaves reasoning effort unset.
  Existing hosted OpenAI routes retain `max_completion_tokens`; explicit output
  field overrides are limited to those two OpenAI-compatible fields.
- The two transport contexts share the existing elapsed gateway deadline.
  Ordered fallback, conservative reservations, reported usage, response-body
  bounds and terminal-status validation retain their existing contracts.
- The model supplies JSON text to the bounded planner/synthesis contracts.
  Native tool calls and reasoning-only replies cannot become successful plans.
  No tool, workflow, approval, lease or database semantics changed.
- Container loopback is local to each container. Host/service endpoints need
  required authentication and explicit credential forwarding. Compose forwards
  the matching RAG generation/router auth controls with `required` defaults.

Examples:

- [Unauthenticated loopback route](../examples/inkling-local.routes.json)
- [Authenticated service route](../examples/inkling-local-authenticated.routes.json)

## Executed evidence

The machine-readable record is [testing-results-inkling-local.json](testing-results-inkling-local.json).

| Gate | Result |
| --- | --- |
| Core unit and integration | **545 passed**, zero skipped; 16 external cases explicitly deselected |
| Core layer breakdown | **269 unit + 276 integration** |
| Actual PostgreSQL storage selection | **14 passed**, zero skipped |
| Actual PostgreSQL RAG/API/worker/MCP process lifecycle | **7/7 passed**, cleanup completed |
| Actual SQLite process resilience | **6/6 passed**, cleanup completed |
| Complete 23-module production line coverage | **90.1842%**, floor 85% |
| Complete production branch coverage | **83.1746%**, floor 75% |
| Repository Ruff gates, whitespace and lock consistency | Passed |

Local HTTP cases verify absent Authorization, a live proxy trap receiving no
requests despite poisoned proxy variables, correct output fields, separate
authenticated fallback, reported usage retention, and actual planner → arithmetic
→ synthesis worker transitions. Configuration tests also prove that ambient
OpenAI/Anthropic/Tinker secret files are not opened.

The independent process gates use real RAG and Agent HTTP servers, independent
workers and shipped MCP bridges. They cover tenant/revision isolation, restart
persistence, feature enforcement, source purge, crash recovery, cancellation,
SSE reconnection, bounded retries and persisted deadlines. Model behavior in
these gates is explicitly offline/synthetic.

Both Agentic and companion RAG production-source fingerprints were unchanged
between the start and finish of the process gates. The record identifies the
base commits and tested working-tree source digests; later documentation and CI
pin updates do not change those production digests. The companion benchmark CLI
subsequently received its dedicated authenticated-local credential default; that
CLI is not invoked by these process gates and is covered by the RAG final suite.
Final CI uses companion commit `ca243630dd24e19a541008965a572e13e85a2728`.
No coverage exclusions or
test skip allowances were added. An upstream Starlette/httpx deprecation warning
remains non-failing.

## Reproduction

Core verification:

```bash
python -m pytest -q -m 'not external' --require-no-skips \
  --cov=agentic --cov-branch --cov-config=pyproject.toml \
  --cov-report=json:reports/testing/coverage-inkling-local.json \
  --test-inventory reports/testing/inventory-inkling-local.json \
  --junitxml reports/testing/junit-inkling-local.xml
python scripts/check_coverage.py --report reports/testing/coverage-inkling-local.json
```

With disposable PostgreSQL databases selected through `AGENT_TEST_POSTGRES_URL`,
`AGENT_E2E_POSTGRES_URL` and `RAGS_E2E_POSTGRES_URL`, run:

```bash
python -m pytest -q tests/test_postgres_concurrency.py \
  tests/test_postgres_accounting.py tests/test_platform.py \
  -m postgres --require-no-skips
python tests/e2e_postgres_v04.py --report reports/testing/inkling-local-postgres-process.json
```

Run SQLite resilience with the two E2E PostgreSQL selectors unset:

```bash
python tests/e2e_resilience.py --report reports/testing/inkling-local-sqlite-resilience.json
```
