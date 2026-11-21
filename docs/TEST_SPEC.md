# Complete test layers, failure recovery and code-quality corrections

Status: specified before implementation of this testing iteration.
Baseline service commit: `7d1616e1d696d74733343e53135f5bc9e11b9e6a`.
Companion baseline: `71c484527e534e893929ada5b694440efd7bb7a9`.

## Goal and baseline

Provide explicit unit, integration and real end-to-end gates, fix reproducible
defects and reduce concrete coupling/validation smells. Retain durable,
at-least-once semantics, tenant authorization, approval separation and bounded
execution. Do not add unsupported agent capabilities or change frozen RAG model
quality targets. Test success is not a general answer-quality claim.

The baseline ordinary suite is 109 passed with 3 external-service skips. With
every `agentic` module included, parent-process line coverage is 2161/2667
(81.0274%) and branch coverage is 725/1068 (67.8839%). Real PostgreSQL and MCP
subprocesses have separate execution evidence; this measurement does not cover
their independent processes. Compare numbers using the same scope.

## T-AG-01: explicit layers and honest resource gates

- Every pytest case has exactly one `unit`, `integration` or `e2e` marker.
  Classify existing mixed files per function; unknown or conflicting cases fail
  collection. Pure schemas/evaluators are unit tests. Actual SQLite/ASGI/local
  provider HTTP tests are integration tests, even when they run quickly.
- Record node IDs, primary layer and resource markers in a collection inventory
  and JUnit artifacts. Dedicated PostgreSQL/Redis/S3 jobs must provision actual
  services. A core selection may explicitly deselect them, but not count them
  as successful execution.
- Strict mode fails any selected skip/xfail/xpass. Missing browser binaries,
  service endpoints, packages or required companions must fail an E2E gate.
- Ordinary unit+integration coverage includes the complete `agentic` package,
  with no new production omissions or coverage exclusions. Minimum line
  coverage is 85%; minimum branch coverage is 75%. Do not lower the floors after
  seeing the result. Publish per-file/aggregate XML, JSON and HTML evidence.

## T-AG-02: focused unit and integration contracts

- Test DAG/schema/tool-policy validation, SQL placeholder and finite-number
  behavior, admission metadata, exact zero and prompt-budget boundaries.
- Exercise actual concurrent SQLite workflow admission, idempotency, claims,
  namespace/tenant isolation and transaction rollback with no extra events.
- Test ordinary tool failure, backoff, retry caps and failed-dependency
  blocking, alongside existing deadline/cancellation/lease fencing.
- Exercise real local model HTTP cancellation and malformed/compressed/oversized
  responses. Ambiguous completion must retain the appropriate reservation;
  protocol errors must not silently route to another provider.
- Test provider-specific usage and cache-token accounting, exact numeric price
  validation, output charges and malformed usage. Extract a small shared parser
  where this removes duplicated nested provider logic without changing budgets.
- Test local artifact corruption, size limits and atomic-write cleanup; retain
  actual S3 evidence. Exercise fixed-window limiter rollover and private key
  storage boundaries where the implementation supports them.

## T-AG-03: concrete code-smell/defect corrections

- A read-only baseline audit reproduced concurrent settlement of one reserved
  usage record emitting multiple `model_usage` events. Make settlement atomic
  and idempotent under the same SQLite transaction discipline used for
  reservation. Exactly one settlement event and one immutable final charge may
  result. Verify the corresponding actual PostgreSQL contract in CI.
- Boolean model prices currently pass numeric validation. Reject booleans and
  invalid/nonfinite prices with explicit configuration errors; preserve valid
  zero and positive numeric prices.
- Browser state currently uses a shared cursor/controller, and submission can
  leave an old stream writing into a new workflow view. Use explicit view/stream
  ownership. Changing workflow or credential, or submitting a new workflow,
  invalidates old asynchronous work before aborting it. Stale reads/finalizers
  must not append old events, replace new results or reset a new stream.
- Improve readability in the touched UI and provider/accounting helpers while
  preserving CSP, credential handling, APIs and user-visible successful flows.
  Avoid unrelated formatting churn or blanket lint suppressions.

## T-AG-04: actual failure-recovery E2E

- Start a real RAG API, Agent API, fixed localhost fault proxy and independent
  worker processes with disposable stores, ephemeral ports and bounded cleanup.
  Child environments must not inherit production provider credentials/routes.
- Hold an actual RAG HTTP request, kill its worker process, allow the genuine
  lease to expire and start a new worker. Verify recovery, attempts, durable
  events and final result over HTTP/MCP, without editing SQL state or replacing
  production tools with in-process mocks.
- Cancel in-flight work over the API, release the delayed response and prove
  that late completion cannot overwrite cancellation or run dependent
  publication. Verify the durable terminal state.
- Exercise actual retry/deadline termination using bounded real HTTP faults.
  Reconnect SSE using an observed Last-Event-ID; require strictly newer events,
  no duplicates and agreement with persisted REST events/terminal state.
- Emit a structured report listing expected and completed checks. Every child
  process is stopped and temporary data removed on success, failure and timeout.

## T-AG-05: real browser E2E

- Use actual provisioned Chromium against the real APIs and worker. Drive the
  page controls for submission, status refresh and streaming; verify successful
  evidence and refusal outcomes, unauthorized behavior and approval separation.
- Keep credentials in page memory/Authorization headers. Confirm they do not
  appear in URLs, localStorage or sessionStorage, and are absent after reload.
- Reproduce switching workflow/submitting while a prior stream remains open.
  The new view must not receive the old workflow's events or cursor. Same-run
  reconnect may use only that run's observed cursor. Credential changes cancel
  the old stream and invalidate stale asynchronous responses.
- Save failure diagnostics and representative screenshots with JSON evidence.
  Do not relax CSP, add production test hooks or silently skip a missing browser.

## T-AG-06: complete CI and review

Retain v0.2/v0.3 HTTP+worker+MCP, actual upstream/admission, PostgreSQL/Redis/S3 and
three-container Compose gates. Add explicit unit/integration jobs or steps and
new process-resilience/browser jobs. Run v0.3 and new applicable resilience
contracts against PostgreSQL as well as SQLite. Pin all companion checkouts to
the exact published RAG implementation being tested. Keep the complete frozen
RAG quality benchmark distinct from functional tests and its existing failed
promotion result visible. Publish final counts, scopes, branch coverage,
concrete smell corrections and unresolved limits in a review report.

Commit this specification before implementing tests and refactors.

## Review amendment: connection setup and PostgreSQL accounting width

Independent review of the specified accounting and resource-lifetime paths
identified two additional concrete defects. An acquired Store SQLite connection
must close even if a setup PRAGMA fails, under the same cleanup contract as the
limiter and RAG Store. Add a tracked real-connection regression.

PostgreSQL's existing schema keeps accounting counters and micro-USD costs in
32-bit INTEGER columns, while valid configured prices and validated provider
counters can exceed that range. Before claiming cross-backend accounting
equivalence, use BIGINT for only model_usage input_tokens, output_tokens,
charged_tokens and cost_microusd. Migrate existing INTEGER accounting columns
inside the existing serialized initialization transaction without dropping data
or widening unrelated columns. Verify both fresh schema and migration using an
isolated real PostgreSQL schema, retain existing records, repeat initialization
idempotently, and reserve/settle values above the 32-bit boundary through Store.
Pure schema translation tests must also prove unrelated integer types and bound
values are preserved. The existing frozen model/runtime behavior stays unchanged.
