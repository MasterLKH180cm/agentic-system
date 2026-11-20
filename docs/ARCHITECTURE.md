# Agentic System v0.2 architecture

This document describes the current implementation. The acceptance contract is
[SPEC_V02.md](SPEC_V02.md); executed checks and unresolved verification are in
[IMPLEMENTATION_REVIEW.md](IMPLEMENTATION_REVIEW.md).

## Service responsibilities

| Component | Responsibility |
| --- | --- |
| Agent API | Authenticate, authorize, resolve immutable definitions and policy, persist submissions, expose results/events/memory/usage |
| Independent workers | Claim ready steps, renew leases, execute typed tools, checkpoint progress, record bounded outcomes |
| Workflow store | Runs, ordered steps, lease tokens, absolute deadlines, checkpoints, definitions, memory, usage reservations, artifact provenance, events |
| RAGs | Document/index lifecycle, authorized retrieval, source citations; its provider calls have a separate cost boundary |
| Model gateway | Agent planning and synthesis, ordered purpose routes, reservations, usage reconciliation, output allowances |
| Artifact backend | Bounded content-addressed JSON objects in local storage or configured S3 |

The default Compose deployment uses three services and local SQLite volumes.
Advanced adapters use explicit process configuration or a Compose override;
the baseline Compose environment does not forward every optional setting.

## Submission and definition snapshots

1. API credentials or a verified JWT determine tenant, roles, scopes, and resource
   attributes. The request body cannot select a tenant or a remote endpoint.
2. The control plane resolves an explicit DAG, an immutable workflow definition,
   an offline plan, a durable model-planning step, or a bounded `agent.run` step.
3. Operator policy intersects with the selected policy and agent definition.
   Namespace attributes and permissions narrow access further. Tool, step, round,
   token, and monetary maxima cannot be raised by a submitted definition.
4. The resolved definition and policy are snapshotted with the run. Later
   registrations do not alter active work. Reusing a definition version fails.
5. Idempotency and queue admission are checked before the new run is committed.
   The same key with different request content fails with `409`.

Model planning happens inside a leased worker step. Its usage is reserved and
recorded against the run, and its validated plan expansion is durable. Explicit
plans support 24 steps; generated subplans support 8. Unknown tools, arguments,
dependencies, and cycles fail validation.

## Durable execution and recovery

A claim records a unique fencing token, lease expiry, attempt count, and an
absolute step deadline. A worker renews its lease while execution is active.
Checkpoint, heartbeat, completion, and failure updates require the current
lease and an active workflow. Cancelled or terminal runs reject late mutations.

Exception retries and expired leases both consume the bounded attempt budget.
The deadline persists across claims, preventing repeated recovery from resetting
the time allowance. The worker applies that remaining deadline to its local
coroutine. Loss of the lease cancels that coroutine when the heartbeat detects
it. Network requests or external effects may already have occurred.

Ready DAG steps execute concurrently up to the per-worker bound. Agent-loop
substeps execute sequentially inside one leased `agent.run` step. Its checkpoint
stores the round, plan, cursor, observations, evaluation, and final result.
Recovery resumes committed progress; it does not replay a deterministic event
history. A response received before a crash but not checkpointed may be repeated.
The delivery guarantee remains at least once.

The default evidence evaluator validates source identities and applies lexical
overlap and numeric-support checks. Its acceptance is a heuristic outcome, not
a semantic proof. Arithmetic evaluation separately recomputes the bounded
expression. Failed evidence can trigger a refined query or a validated model
replan, up to the snapshotted round limit. Insufficient evidence, budget,
deadline, and policy stops are recorded as unsuccessful outcomes.

## Authorization and effects

- Requester and approver credentials are disjoint. Verified JWTs check signature,
  algorithm, issuer, audience, expiry, subject, and configured tenant. Roles grant
  named permissions; optional scopes and namespace attributes only narrow them.
  A requester cannot approve their own run with the same JWT subject.
- Each tenant has a distinct configured RAG credential mapped to the same tenant
  in RAGs. The RAG URL is operator-owned. Retrieval requests use `generate:false`;
  returned evidence is bounded and treated as untrusted data.
- Results, events, duplicate submissions, memory, usage inspection, and artifacts
  enforce their relevant tenant and namespace boundaries. Memory provenance must
  refer to an existing authorized tenant run; TTL excludes expired records.
- Typed tools validate arguments again at execution. Adaptive loops may use only
  the registered read-only subset. Retrieved text and model plans cannot grant
  tools, choose arbitrary URLs, or expand budgets.
- `artifact.publish` always requires a separate approval. It stores bounded JSON
  in a content-addressed backend and records the producing run and namespace.
  It does not send email, publish a public post, or initiate a payment.
- Arithmetic uses an allowlisted AST interpreter. This is not a general execution
  sandbox; the tools endpoint reports arbitrary-code execution as unavailable.

The browser UI keeps credentials in page memory and renders returned data as
text. Authenticated SSE rechecks credentials, reads durable event IDs, and accepts
a reconnect cursor. It does not put credentials in a URL. Signed hooks use
operator-bound tenants, HMAC over timestamp and raw body, a five-minute freshness
window, and stored replay receipts before normal workflow validation.

## Model accounting boundary

The gateway routes calls by purpose and ordered operator configuration. It falls
back only for transport failures, `429`, and transient `5xx`. Each attempt first
atomically reserves a conservative UTF-8-byte input estimate plus requested
output allowance. Concurrent reservations share the run budget. The estimate is
not a model-token count.

Valid provider usage replaces the reservation. Missing usage and ambiguous
failures retain explicit estimated charges. Monetary estimates require both
operator-supplied input/output prices; a dollar budget with missing prices is
rejected before the request. Incomplete pricing returns `estimated_cost_usd:null`
and a separate `known_cost_usd` subtotal. Provider usage may differ from the
reservation, and the ledger is not an authoritative billing statement.

The ledger covers agent planning and synthesis. The deterministic evaluator
makes no model call. RAG embeddings, model-assisted retrieval, and any other
external service usage are outside this ledger. A gateway budget is therefore
not a whole-system spending cap.

## Persistence and optional services

| Backend | Implemented behavior | Operational boundary |
| --- | --- | --- |
| SQLite | WAL, transactions, migration of existing workflow columns | API and workers must share a local file/volume |
| PostgreSQL | Same Store contract through bound SQL parameters; database-local advisory lock serializes transactions | Shared-state correctness verified; no `SKIP LOCKED` queue or throughput claim |
| Local artifacts | Tenant-keyed paths, atomic writes, SHA-256 integrity check | Shared artifact storage required across readers/writers |
| S3-compatible artifacts | Configured bucket, bounded requests, tenant-keyed object names, integrity verification | Requires `storage` extra; real-service verification remains pending |
| SQLite rate limiter | Atomic fixed-minute counters shared through a local limiter file | A PostgreSQL workflow DSN does not make the default limiter distributed |
| Redis rate limiter | Atomic shared counter/expiry through Redis | Rate limiting only; real-service verification remains pending |
| OTLP HTTP | Optional safe-attribute traces, operation counts and duration metrics | Requires telemetry extra and a collector; export delivery depends on that collector |

Configuration never silently substitutes SQLite for a configured PostgreSQL
store or local files for a configured S3 backend. Adapters validate required
dependencies and settings; Redis and S3 also check connectivity at initialization.
Telemetry fails on missing configuration/dependencies, while collector delivery
is handled by the exporter.

## Observability and deployment limits

Events store structured execution metadata and stopping reasons. Tool failures
persist exception types rather than provider bodies. Telemetry excludes source
text, prompts, credentials, and exception messages from attributes. Protected
run records intentionally retain results, citations, checkpoints, and memory;
they require retention policy, database access controls, and backups.

The implementation does not provide a managed secret vault, tamper-evident audit
chain, external task queue, worker autoscaler, deterministic history replay,
general code sandbox, or semantic memory index. PostgreSQL transactions are
serialized for correctness; fairness, saturation behavior, failover, and
multi-host operations require deployment-specific validation. TLS, identity
provider administration, secrets provisioning, and backup recovery remain
operator responsibilities.
