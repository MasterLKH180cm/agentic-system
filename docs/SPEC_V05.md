# v0.5: concurrent PostgreSQL execution and explicit model capabilities

## Problem and boundary

The v0.4 PostgreSQL adapter opens a TCP connection and takes one database-wide
advisory lock for every transaction, including API reads. Independent workflows
therefore wait for unrelated work. Simply removing that lock would break the
transactional cancellation, lease, dependency, and model-budget invariants.

This revision keeps the existing Python execution plane and SQLite development
contract. It introduces bounded connection reuse and serialization at the run
boundary. It also makes the Inkling 2 Mini request settings explicit. Performance
results must identify real service/model calls separately from fixtures.

## Persistence contract

- A process owns its bounded psycopg connection pools; pool size, checkout timeout
  and waiter count are validated settings. Size zero selects direct connections
  for comparison. A pool created before fork cannot be reused by its child.
- Read operations use a PostgreSQL read-only repeatable-read transaction without
  the global write lock. A workflow and its steps are read from one snapshot.
- Workflow mutations first take a shared compatibility lock on the legacy
  advisory key, then lock the tenant-scoped run row before touching steps, events, reservations
  or references. This serializes one workflow's state transitions while allowing
  unrelated runs to proceed. The lock order is always run, then subordinate rows.
- PostgreSQL claims select eligible runs with `FOR UPDATE SKIP LOCKED`. Eligibility
  includes recoverable expired leases, ready dependency-satisfied pending steps,
  and elapsed persisted deadlines. A locked workflow never stalls claims for a
  different workflow. The SQLite claim implementation retains its write lock.
- Recovery, approval requests, attempt limits, persisted deadlines, fencing
  tokens, current run status and events preserve their existing semantics.
- Every owned execution mutation checks the persisted deadline after acquiring
  its database write locks. Expired completion, failure, heartbeat, checkpoint,
  plan expansion and artifact attachment instead commit the terminal deadline
  outcome; they cannot publish late success or new work. Cancellation and stale
  lease fencing take precedence. Reported model usage can still settle after
  expiry, because a deadline does not erase an incurred charge.
- The worker checks both elapsed local time and the persisted absolute deadline
  after execution, including synchronous handlers that cannot yield to an async
  timer. Database lock waits can delay finalization but cannot admit late results.
- Schema initialization and admission/definition control operations keep short
  exclusive serialized transactions. Their lock also bridges old v0.4 writers
  safely with new shared-lock workers during a rolling deployment. This is not a general distributed queue or autoscaler.
- Connection failure is explicit; no PostgreSQL-to-SQLite fallback is allowed.

## Model contract

- An `inkling` preset uses the OpenAI-compatible transport and canonical
  `thinkingmachines/Inkling-2-Mini` identifier. Endpoint and credentials are
  operator configuration; no secret is committed or printed.
- OpenAI-compatible routes can set validated `reasoning_effort` values. Inkling
  uses `max_completion_tokens`; `response_format` is not sent because its Tinker
  API ignores that field. Existing planner/synthesis Pydantic/JSON validation
  remains authoritative.
- `finish_reason=length` and Anthropic `stop_reason=max_tokens` are failures, even
  if the truncated content happens to parse as JSON. Reported usage is settled
  before rejecting content. Reasoning text is never substituted for an answer.
- `AGENT_MODEL_TIMEOUT_SECONDS` defaults to 90 and accepts finite values in
  0.1..360 seconds. One elapsed deadline covers the entire gateway call, including
  connection, headers, body and fallback attempts; slow response dribbling cannot
  reset it. The worker's remaining step deadline can cancel it sooner. Response
  bodies remain identity-encoded and bounded to 1,000,000 bytes. Timeout retains
  the conservative usage reservation and does not start another fallback route.
- Embeddings remain a separate RAG responsibility. The generation model change
  does not change index dimensions or require a hidden reindex.

## Acceptance evidence

1. Existing unit/integration tests and line/branch coverage gates pass.
2. Actual PostgreSQL tests prove connection reuse, independent snapshot reads,
   simultaneous unique claims, skip-locked progress, cancellation fencing,
   bounded lease recovery, mixed-version locking, foreign-tenant lock isolation,
   and atomic concurrent reservations/settlements.
3. Provider transport tests exercise reasoning settings, output cap, malformed
   output, reasoning-only replies, truncation and reported usage on failure.
4. Real two-service API/worker smoke covers the modified execution path.
5. A live Inkling benchmark requires an accessible authorized endpoint/key. A
   missing endpoint or credential is recorded as blocked, never a fabricated
   throughput/quality measurement.

## Sources

- https://tinker-docs.thinkingmachines.ai/inference/openai/overview/
- https://tinker-docs.thinkingmachines.ai/inference/openai/chat-completions/
- https://thinkingmachines.ai/model-card/inkling-2-mini/
- https://www.postgresql.org/docs/17/sql-select.html
- https://www.psycopg.org/psycopg3/docs/advanced/pool.html
