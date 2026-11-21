# Request handling and dashboard retry polish

Status: specified before implementation on 2026-11-21.
Baseline: `8b18c131f2e8e60f6f90522d59c909fb4b87690b`.

## Goal and scope

Correct reproduced request-boundary failures, make dashboard errors actionable
and preserve one logical submission across an ambiguous transport failure.
Keep durable workflow semantics, approval separation, tenant scope, bounded
execution, existing asynchronous ownership guards and model quality boundaries.
Use the existing tests/CI gates; do not introduce unrelated features or gates.

## P-AG-01: webhook authentication failures

The baseline returns HTTP 500 for a non-ASCII digit timestamp (conversion after
`str.isdigit`) or a non-ASCII signature (string HMAC comparison). These are
invalid caller credentials, not internal processing failures.

- Validate the bounded timestamp as ASCII decimal before conversion and the
  signature as the documented lowercase `sha256=` plus 64 hexadecimal digits.
  Unknown hooks, malformed/expired timestamps and malformed/wrong signatures
  must return a fixed HTTP 401 response appropriate to the failure.
- Rejected requests must not create workflows, receipts or limiter entries.
  Keep the existing body-size/deadline checks, constant-time digest comparison,
  raw-byte signature convention, tenant binding and replay/idempotency behavior.
- Test malformed headers through an ASGI transport that preserves raw bytes,
  and real HTTP in the existing two-service E2E. Retain valid signed execution,
  exact-delivery replay rejection and logical retries with a fresh signature.

## P-AG-02: safe request validation responses

The baseline also returns HTTP 500 when an invalid workflow contains a
nonfinite cost or an unpaired Unicode surrogate: the default validation handler
echoes invalid input into its response.

- Return HTTP 422 with structured `type`, `loc`, `msg` details only; omit raw
  input and exception contexts. Make error locations/messages serializable
  even for malformed Unicode, without changing valid Unicode input.
- Scope the handler to request validation; preserve authentication, policy,
  conflict and unexpected-error behavior.
- Regressions must use the actual application and SQLite, retain useful field
  locations/messages, and prove no workflow/state mutation on rejection.
- Reject nonfinite numbers and invalid Unicode inside memory metadata before
  writes. The baseline can persist an unpaired surrogate and make subsequent
  reads fail; a rejected replacement must preserve the existing record. Keep
  the existing metadata size limit and legitimate Unicode content unchanged.

## P-AG-03: actionable dashboard errors and idempotent retry

- Format bounded structured field-validation messages instead of displaying
  `[object Object]`. Preserve ordinary string details and provide a useful HTTP
  status fallback for non-JSON or malformed error responses. Render as text.
- Generate a cryptographic idempotency key for a logical submission and retain
  it only in page memory while the result is unresolved. Reuse it when retrying
  the same payload and credential after an ambiguous failure, including a lost
  committed response. Clear it after acknowledged success or a goal, execution
  mode or credential change. An obsolete response must not clear newer state.
- Keep intentional new submissions after acknowledged success distinct. Keys
  and credentials must not enter URLs or persistent browser storage. Preserve
  current stream/cursor/view ownership and the hash-authorized CSP.
- Extend the actual Chromium gate with a real field-validation error, a
  non-JSON failure fallback, and a real committed POST whose response is
  deliberately discarded before a UI retry. Verify one durable run/execution,
  same ID and duplicate recognition; verify changed inputs create a new logical
  submission. Transport fault control must not fabricate API success/state.

## P-AG-04: coherent configuration and evidence

- Fix production snippets whose sample credentials are shorter than the
  enforced minimum and whose requester/RAG/approver tenant mappings disagree.
  Provide generated, distinct credentials and one coherent two-service example.
- Explain which settings must reach both the API and worker and distinguish
  the two mapping directions. Keep development defaults and deployment scope
  accurate; do not commit real secrets.
- Refresh README/test evidence without representing a historical successful
  job as current-revision execution. Preserve current flowcharts and boundaries.

## Acceptance and review

Run strict unit/integration selections, lint, complete-package coverage floors
(85% statements, 75% branches), and actual RAG/API/worker/MCP smoke. Retain all
seven current CI jobs, including actual PostgreSQL/Redis/S3, Compose, Chromium,
research and admission execution. Pin companion RAG checkouts to the exact
published implementation. Update measured results/source hashes and review the
final diff. Local Chromium remains unavailable under this workspace's socket
policy; current-revision provisioned CI must execute the browser regressions.
