# Testing and code-quality review

Measured locally on 2026-11-21. The [test specification](TEST_SPEC.md) and
[request/dashboard polish specification](POLISH_SPEC.md) were committed before
their respective implementations. The comparison baseline is
`7d1616e1d696d74733343e53135f5bc9e11b9e6a`; the exact baseline measurement is in
[testing-baseline.json](testing-baseline.json). Current measurements and
production-file hashes are in [testing-results.json](testing-results.json).

**Local core validation passes: 127 unit cases and 173 integration cases,
300 unique cases with zero selected skips.** All 21 production Python modules
remain in scope. Statement coverage is **2379/2697 = 88.21%** and branch
coverage is **875/1078 = 81.17%**, above the specification's separate **85%**
and **75%** floors. No production coverage exclusions were introduced.

The v0.2 and v0.3 real HTTP/worker/MCP scripts passed again after the polish;
the retained six-check SQLite resilience gate has separate execution evidence.
The recorded [baseline Chromium job](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44883239875/job/136782891230)
passed the previous **8/8 checks**. The expanded browser gate now requires twelve
checks on the exact PR revision. Local Chromium remains unavailable under this
environment's UNIX-socket restriction. Actual browser, external resources,
Compose, research and admission remain required; inspect the current PR checks
and artifacts for their final outcomes.

## Measured core results

| Measurement | Baseline | Current local result |
| --- | --- | --- |
| Ordinary/core cases passed | 109, with 3 external checks skipped | 127 unit + 173 integration = 300 unique passes |
| Collection scope | Historical ordinary suite | 305 cases: 300 core + 5 external |
| Unit selection | No explicit per-case inventory | 127 selected; 178 deliberately deselected |
| Core integration selection | No explicit per-case inventory | 173 selected; 132 deliberately deselected |
| Selected skips, xfails or xpasses | 3 resource skips in historical ordinary run | 0 in the split core gates |
| Statement coverage | 2161/2667 = 81.03% | 2379/2697 = 88.21% |
| Branch coverage | 725/1068 = 67.88% | 875/1078 = 81.17% |
| Production Python files measured | Complete baseline package | All 21 current files |
| Coverage exclusions | 0 | 0 |

These are parent-process Python measurements from the separately selected unit
and integration suites. Independent worker/MCP processes, browser JavaScript
and actual external-service execution have separate reports. Their execution
is not added to these coverage numbers. Coverage.py's combined percentage is
also not used as a substitute for the two independent floors.

The generated evidence is `reports/testing/inventory-unit.json`,
`inventory-integration.json`, `junit-unit.xml`, `junit-integration.xml`,
`coverage-core.json`, `coverage-core.xml` and `coverage-html/`. CI publishes
these as `agentic-core-test-evidence`. The committed result summary retains
the counts, raw coverage fractions and production source hashes without
committing generated coverage output.

## Test layers and strict resource gates

[Root conftest.py](../conftest.py) registers custom pytest options early,
including when the report directory already exists. Each collected case must
have exactly one primary `unit`, `integration` or `e2e` layer. Existing mixed
files use the explicit per-function [manifest](../tests/test_layers.json);
new files declare their layer directly. Missing or conflicting classification
fails collection, and a resource-dependent case cannot call itself a unit test.

Inventory entries retain full parametrized node IDs, layer, resource markers
and selected/deselected status. JUnit records the layer and resources as test
properties. `--require-no-skips` fails runtime skips, xfail/xpass and
collection-time skips. The five external cases are deliberately deselected
from the local core command, not counted as successful service execution.
The strict gate and inventory behavior have their own regression suite in
[test_testing_contracts.py](../tests/test_testing_contracts.py).

[check_coverage.py](../scripts/check_coverage.py) requires every production
Python module, nonempty statement and branch measurements, the recorded
baseline exclusion policy and both numerical floors. New omissions, coverage
suppression pragmas or custom exclusion rules fail the gate. The test suite
checks these failures as well as each floor independently.

The process and browser E2E scripts are separate executable gates. Their
expected/completed check lists are not added to the pytest case count. The
[README test flow](../README.md#validation-and-limits) shows how core coverage,
real prerequisites and E2E evidence join in review.

## Corrections and regression evidence

| Concrete defect or coupling | Change | Regression evidence and scope |
| --- | --- | --- |
| Concurrent settlement could read the same reserved usage record and emit multiple final accounting events | [Store.settle_usage](../agentic/store.py) begins the serialized transaction before reading reservation state; the first final charge and event remain immutable | Concurrent two-store SQLite settlement in [test_persistence_concurrency.py](../tests/test_persistence_concurrency.py); corresponding actual PostgreSQL contract in [test_platform.py](../tests/test_platform.py) is required in CI |
| Python booleans passed numeric route-price validation; extremely large integers could reach `math.isfinite` and raise the wrong error | [config.py](../agentic/config.py) checks exact numeric types and allowed range before finite conversion; valid zero prices remain valid | Invalid booleans, nonfinite/negative/out-of-range values, huge integers and valid price boundaries in [test_contracts_unit.py](../tests/test_contracts_unit.py) |
| Provider usage validation was embedded in the response path | [reported_usage](../agentic/model.py) centralizes bounded integer validation and Anthropic cached-input accounting; incomplete/malformed usage retains the conservative reservation | Pure parser cases plus actual local HTTP cancellation, malformed/compressed/oversized responses, output limits and provider charges in [test_model_failures.py](../tests/test_model_failures.py) |
| Valid token totals and microdollar charges could exceed PostgreSQL's 32-bit accounting columns | [postgres.py](../agentic/postgres.py) widens only `model_usage.input_tokens`, `output_tokens`, `charged_tokens` and `cost_microusd` to `BIGINT`; Store initialization migrates existing narrow columns in one serialized transaction | [test_postgres_schema.py](../tests/test_postgres_schema.py) verifies targeted, idempotent translation/migration and unchanged bound values. [test_postgres_accounting.py](../tests/test_postgres_accounting.py) requires actual fresh and legacy PostgreSQL schemas, preserved records and large reservations/settlements in CI |
| PostgreSQL dictionary rows collapsed two unaliased `COALESCE` aggregates into one column | Reservation queries name token and cost totals separately and access them by name; aggregate integers are normalized consistently across backends | The first actual PostgreSQL CI run exposed the failure. Mapping-row budget checks and retained real PostgreSQL reservation, settlement and migration contracts cover the correction |
| SQLite's transaction context did not itself close rate-limiter connections | [auth.py](../agentic/auth.py) combines explicit closing with transaction commit/rollback in initialization and requests | Local concurrency, rollover, hashed bucket storage, success/error cleanup and initialization-failure tests in [test_artifacts_local.py](../tests/test_artifacts_local.py) |
| Store connection setup occurred before its cleanup-protected block | [store.py](../agentic/store.py) protects row-factory/PRAGMA initialization with the same rollback/close path | Setup-failure cleanup and actual concurrent SQLite persistence checks in [test_persistence_concurrency.py](../tests/test_persistence_concurrency.py) |
| A shared UI cursor/controller let previous streams and fetches write into a newer workflow/credential view | [index.html](../agentic/static/index.html) gives views and streams explicit ownership; generation changes before abort, results are guarded after awaits, and old action finalizers cannot reset a newer action | [e2e_browser.py](../tests/e2e_browser.py) drives actual workflow, credential and submission switches, same-run reconnect, late status responses and late submissions. Actual Chromium CI passed 8/8 ownership/workflow checks; local syntax/API fixture checks are recorded separately |

The PostgreSQL migration deliberately leaves unrelated integer columns,
quoted SQL text and bound values unchanged. Existing data and repeat startup
are part of the actual-service contract. Migration runs during Store
initialization; this iteration does not establish production-size migration
latency or PostgreSQL failover behavior.

Base Ruff checks and the production `B,RUF021` checks passed locally. The
review addresses the concrete defects above; it does not claim that all
possible code smells or defects have been eliminated.

## Request and dashboard polish

The follow-up specification precedes its implementation. It adds **32 pytest
cases: six unit and 26 integration**, with actual ASGI/SQLite state checks. The
complete core counts above include them. An independent focused review reran
these cases plus the retained valid webhook/fresh-signature/replay contract:
**33 passed, no skips**. The updated real HTTP v0.2 script also proves malformed
authentication and unsafe JSON cannot add workflows or replace valid records.

| Reproduced problem | Correction and review evidence |
| --- | --- |
| Non-ASCII webhook timestamp/signature caused 500 | Bounded ASCII timestamp and signature-shape validation returns fixed 401 before limiter, receipts or submissions; valid constant-time HMAC verification and replay protection remain intact |
| Rejected values broke the default validation response; nested memory metadata could poison later reads | A scoped 422 handler omits input/context and escapes invalid error-string scalars; memory metadata is validated before writes while preserving the previous size rule and original records |
| The dashboard displayed structured errors as `[object Object]` | Bounded field/message formatting preserves useful validation details and handles non-JSON HTTP failures without displaying raw intermediary HTML |
| A lost successful submission response let a UI retry create a second run | A cryptographic memory-only key follows the unresolved payload/credential; actual committed responses are discarded before retry, with duplicate recognition and one durable execution |
| A corrupted successful acknowledgement could prematurely discard the retry key | Independent review reproduced a lone-surrogate ID failure; canonical UUID and boolean acknowledgement validation now retains uncertain state for malformed responses, including path/control IDs |
| Production examples disagreed about tenant identity and used short sample tokens | Exact README bootstrap/shell commands generated distinct credentials, coherent maps and owner-only files; real settings/authentication and repeat-run non-overwrite checks passed in paths with spaces |

Successful acknowledgements or changed logical inputs create a new submission
identity. Repeating an unchanged input event preserves it. Obsolete responses
cannot clear newer state. Reload discards in-memory credentials and pending
identity, so the README instructs users to inspect existing runs before
resubmitting after reload. Browser execution remains a separate required gate;
Node handler probes and API tests are not counted as Chromium checks.

## Actual process and transport evidence

[e2e_support.py](../tests/e2e_support.py) starts the actual RAG API, Agent API,
independent worker and a fixed localhost fault proxy. Services use disposable
stores, ephemeral ports and fixture credentials. The child environment is an
allowlist, so ambient provider keys and routes are not inherited. The existing
v0.2/v0.3 harnesses use the same allowlist while explicitly preserving their
caller-selected research endpoint, admission profile and PostgreSQL settings.

The proxy forwards actual RAG requests and controls only transport failure or
response release. It does not replace production tools or retrieval results.
The new [resilience gate](../tests/e2e_resilience.py) passed all six checks with
SQLite in 17.644 seconds:

| Check | Assertion demonstrated |
| --- | --- |
| Worker crash recovery | Kill the real worker with `SIGKILL`; let its genuine lease expire; a new worker completes the run. The committed dependency executes once, the interrupted read has two attempts, and its original deadline remains unchanged |
| In-flight cancellation | Cancel through the API, then release the delayed real upstream response. The cancelled state survives, no read result is committed, and dependent publication has zero attempts |
| SSE reconnect | Use an actually observed `Last-Event-ID`; receive only newer unique events equal to persisted REST events and the same terminal state |
| Transient retry | A real HTTP 503 causes one durable retry, followed by an actual successful RAG request |
| Retry exhaustion | Repeated real HTTP failures stop at the configured two attempts and do not execute dependent publication |
| Persisted deadline | A held actual response exceeds the step deadline, produces a typed deadline result, and does not retry or publish |

Recovery, cancellation and deadline results are also inspected through the
actual stdio MCP bridge. No workflow SQL state is edited to simulate a crash,
lease expiration or cancellation. Success cleanup removed temporary data and
reaped children. A separate local injection of an unavailable startup service
also confirmed cleanup of both created children and all temporary data.

The updated [v0.2 gate](../tests/e2e_v02.py) passed its seven lifecycle check
groups, and [v0.3](../tests/e2e_v03.py) passed its three evidence/terminal-state
groups again with SQLite. These check groups overlap some existing contracts;
they are reported separately from the 300 pytest cases.

## Browser evidence and remaining CI gates

The browser gate requires twelve completed checks:

1. Submit, refresh and stream a successful workflow with actual source citations.
2. Preserve the typed insufficient-evidence refusal and its terminal failed state.
3. Reject invalid credentials and cross-tenant inspection without retaining private results.
4. Deny requester approval and publish the verified arithmetic artifact through a distinct approver.
5. Fence streams/cursors when submitting, switching workflow or changing credential; reuse only a same-run observed cursor.
6. Discard an older genuine status response delivered after the selected workflow changes.
7. Discard an older genuine submission response delivered after credentials/view change, while proving the backend submission actually completed.
8. Keep credentials in Authorization headers/page memory, absent from URLs, request bodies, browser storage and the reloaded page.
9. Show useful real field-validation errors and bounded non-JSON error fallbacks; a later rejection must preserve an earlier uncertain identity.
10. Discard a real committed POST response, then retry the unchanged form with the original key; recover the same run and one execution.
11. Recover real committed submissions after seven malformed acknowledgement variants, including invalid Unicode/path/control IDs, with no repeated execution.
12. Start fresh identities after acknowledged success or changed inputs, fence obsolete acknowledgements, and keep pending keys out of storage and reloads.

The two stale-response cases buffer genuine API responses at the browser
transport; they do not fabricate service results or add production test hooks.
The original script-hash CSP remains enforced. The harness records uncaught
JavaScript errors, expected/completed checks, browser version, cleanup status
and only screenshots created during that invocation.

Local Chromium exists, but startup fails because UNIX sockets are prohibited
in this execution environment; the managed-browser download also returned
HTTP 403. The local browser report records **failure, 0/8 completed**, with
services cleaned up. That local run supplies no screenshot or visual-interaction success. The separate
baseline CI Chromium run did execute all eight checks, produce four screenshots and
report zero page errors with cleanup completed.

The new actual-script Node DOM probes used real RAG/API/worker services to
verify submission behavior and seven malformed acknowledgement variants. Their
reports are `polish-dashboard-handler-local.json` and
`polish-acknowledgement-local.json`; the latter recovered seven actual commits
in fourteen POSTs with zero repeated executions. These probes are not browser
evidence. The twelve-check Chromium report is required for this revision.

The first expanded [Chromium CI run](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44898791285/job/136830038536)
completed **11/12 checks**, including all seven malformed acknowledgements,
before the final overlap case raised `Route is already handled`. The two
one-use interceptors expired while an earlier genuine response was still held;
Playwright removed the last interception pattern and resumed that pending
route before the harness tried to fulfill it. One persistent dispatcher now
owns both overlapping requests until their actual responses settle, then is
explicitly removed. The newer response must still be committed and discarded,
the obsolete acknowledgement must not affect its view or retry identity, and
the final retry must recover the same run with one execution. All twelve
checks remain required on the corrected revision; the failed run is not
counted as completed browser evidence.

| Required current-revision CI gate | Evidence required before claiming completion |
| --- | --- |
| Core unit/integration | Inventories, JUnit, complete-package JSON/XML/HTML coverage and both fixed floors |
| SQLite process recovery | Six completed checks in `resilience-sqlite.json` |
| Actual Chromium | Twelve completed checks in `browser.json`, representative screenshots and failure diagnostics when applicable |
| PostgreSQL/Redis/S3 | All five external pytest cases execute without skips; actual accounting migration/large charges, concurrency, Redis and S3 behavior |
| PostgreSQL v0.2/v0.3/resilience | Real HTTP, worker and MCP flows; separate disposable databases prevent unfinished work from another harness affecting claims |
| Docker Compose | All three actual service containers and their external HTTP workflow |
| RAPTOR and LightRAG | The retained pinned upstream → RAG HTTP → Agent worker/MCP chains |
| Frozen Qwen admission | The retained two calibration cases through actual profile scoring, admission HTTP, independent worker and MCP; this is an integration gate |

The [CI workflow](../.github/workflows/ci.yml) provisions these resources and
pins all Agent companion checkouts to RAG commit
`f62e088a58c0bbcbda84d8db4df51b6cc84b863f`. The first remote execution exposed duplicate PostgreSQL aggregate labels and a
fixture URL-encoding defect. Distinct SQL aggregate aliases preserve both token
and cost totals; percent-encoded connection options preserve the disposable
schema path. Both actual migration/accounting cases remain required on the
corrected revision. The exact PR checks and artifacts record the final outcome. The earlier
[v0.3 ledger](IMPLEMENTATION_REVIEW_V03.md) remains historical evidence.

## Reproduce the measurements

Run from the repository root with the tested companion at `../RAGs`:

```bash
python -m pip install -e ".[dev,mcp,telemetry,e2e]" -e "../RAGs[mcp]"
export AGENT_ENV=test RAGS_ENV=test

python -m pytest -q -m unit --require-no-skips \
  --test-inventory reports/testing/inventory-unit.json \
  --junitxml reports/testing/junit-unit.xml \
  --cov=agentic --cov-branch --cov-config=pyproject.toml \
  --cov-report=json:reports/testing/coverage-unit.json

python -m pytest -q -m "integration and not external" --require-no-skips \
  --test-inventory reports/testing/inventory-integration.json \
  --junitxml reports/testing/junit-integration.xml \
  --cov=agentic --cov-branch --cov-config=pyproject.toml --cov-append \
  --cov-report=json:reports/testing/coverage-core.json \
  --cov-report=xml:reports/testing/coverage-core.xml \
  --cov-report=html:reports/testing/coverage-html

python scripts/check_coverage.py --report reports/testing/coverage-core.json
ruff check agentic tests scripts conftest.py --select E4,E7,E9,F
ruff check agentic --select B,RUF021

python tests/e2e_v02.py
python tests/e2e_v03.py
python tests/e2e_resilience.py --report reports/testing/resilience-sqlite.json

python -m playwright install --with-deps chromium
python tests/e2e_browser.py \
  --report reports/testing/browser.json \
  --artifacts reports/testing/browser-artifacts
```

For an explicitly selected preinstalled Chromium, add
`--browser-executable /absolute/path/to/chromium`; the harness does not silently
fall back or skip if it cannot launch the selected browser.

Provision actual disposable PostgreSQL, Redis and S3-compatible services as in
CI, install `.[postgres,storage]`, and configure `AGENT_TEST_POSTGRES_URL`,
`AGENT_TEST_REDIS_URL`, `AGENT_TEST_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION`. Then execute:

```bash
python -m pytest -q -m "integration and external" --require-no-skips \
  --test-inventory reports/testing/inventory-storage.json \
  --junitxml reports/testing/junit-storage.xml

# Set AGENT_E2E_POSTGRES_URL to a separately created disposable recovery database.
AGENT_E2E_POSTGRES_URL="${AGENT_E2E_POSTGRES_URL:?Set the disposable recovery database URL}" \
  python tests/e2e_resilience.py --report reports/testing/resilience-postgres.json
```

The same environment variable selects PostgreSQL for v0.2 and v0.3. CI creates
one database per harness. Selecting an external case without its package,
endpoint or running service is a failure; it is not equivalent to the explicit
local core selection.

## Quality and operational boundaries

The frozen companion Qwen quality result remains failed and disabled by
default: **9/16 held-out unanswerable cases refused**, and admitted hybrid
Recall@3 fell from **0.85 to 0.70**. The retained two-case admission job checks
integration and refusal propagation; it does not rescore the holdout, retune
thresholds or promote the profile. See the
[measured RAG quality report](https://github.com/MasterLKH180cm/RAGs/blob/c015d300ad183117b22f7329bd697ccd93287fcd/reports/admission-verification-qwen-v03.json).

Execution remains at least once. A crash before checkpointing can repeat a
tool request; cancellation cannot reverse an external effect already completed.
The new process tests demonstrate durable fencing and bounded recovery for
their fixtures, not exactly-once side effects.

The documented twelve-layer boundaries remain: heuristic source/citation and
number checks, sequential loop substeps, lexical memory, no general code
sandbox, no external task queue or autoscaler, no deterministic history-replay
engine and no tamper-evident audit chain. Agent gateway budgets cover Agent
planning/synthesis, while RAG scoring, embeddings and upstream generation have
separate resource/cost boundaries.

SQLite assumes a shared local volume; PostgreSQL still serializes queue
transactions with an advisory lock. Disposable-service tests establish their
asserted integration behavior. They do not establish production throughput,
fairness, failover, live identity-provider administration, paid-provider answer
quality or semantic truth of every generated claim.
