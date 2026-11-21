# Agentic System v0.2 specification

Status: implementation contract prepared and committed before feature code, following the requested specification, implementation, and self-review sequence.
Baseline: v0.1 at a32d3dff2c5b57f44800dfe3d8b2a726e1525db0.
Companion: RAGs/docs/SPEC_V02.md.

## 1. Goal and delivery contract

Extend the static local DAG runtime into a bounded, observable agent runtime while preserving durable DAGs, separate approval credentials and tenant-specific RAG access. Map every original layer to concrete behavior and evidence. A directory or class name alone does not complete a layer.

Default local execution remains possible without paid models. Provider fixtures demonstrate protocol/control behavior, not paid-provider or model-quality verification. Optional OIDC, PostgreSQL, object storage, Redis and telemetry adapters must report configuration/verification status. Missing required capabilities fail explicitly. Do not claim a twelve-layer production deployment from a SQLite demo.

## 2. Twelve-layer functional requirements

| ID | Layer | Required behavior |
| --- | --- | --- |
| AG-01 | Interface | Existing REST plus resumable authenticated SSE, a minimal status/submit UI, and authenticated signed inbound workflow hooks with replay protection. |
| AG-02 | Identity & Gateway | Tenant-bound API credentials, disjoint approval role; optional validated OIDC JWT/JWKS; named permissions and policy checks; bounded rate limiting. |
| AG-03 | Control Plane | Immutable versioned agent, workflow and policy definitions; runs snapshot resolved definitions so edits never change active work. |
| AG-04 | Orchestration | Durable run/step/loop checkpoints, fencing, lease renewal, bounded expiration recovery, deadlines and cancellation guards. |
| AG-05 | Agent Runtime | Plan -> execute -> evaluate -> bounded replan loop, persisted observations and stopping reason. Static DAG remains supported. |
| AG-06 | Parallel Execution | Dependency-aware parallel workers with bounded batches, failure/lease caps and cancellation; distributed queue/control extensions explicitly identified. |
| AG-07 | Model Gateway | Purpose-based configured routing, transient-error fallback, preflight token/cost reservation, usage reconciliation, output caps and honest cost estimates. |
| AG-08 | Tool & Integration | Typed registered tools and argument validation, tenant/policy authorization, MCP and fixed RAG adapters. Adaptive loops use read-only tools; general code sandbox is a separately configured capability. |
| AG-09 | Knowledge & Memory | Tenant/namespace-isolated memory CRUD/search with TTL/provenance and bounded recall in loops; document retrieval remains in RAGs. |
| AG-10 | Persistence | Migration-safe local store plus explicitly configured PostgreSQL / object-store / Redis integrations where supplied; no unverified persistence claim. |
| AG-11 | Governance | Versioned tool/memory/budget policies, separate HITL approvals, secret configuration validation and redacted append-only events. |
| AG-12 | Observability & Quality | Correlated spans/metrics when OTel is configured, structured execution/usage records and task-level outcome evaluation; quality evidence separate from liveness. |

## 3. API and domain contracts

WorkflowIn retains goal/steps/idempotency_key and adds runtime_mode (dag|agent_loop, default dag), optional agent/policy IDs and versions, memory_namespace, max_rounds (default 3, max 5), max_model_tokens (default 12000) and optional max_cost_usd. Explicit DAGs remain bounded to 24 steps; generated subplans to 8.

AgentDefinition stores immutable tenant/name/version, instructions, allowed tools, evaluator (evidence|arithmetic), memory namespace and round limit. WorkflowDefinition stores immutable validated steps. PolicyDefinition stores allowed tools/namespaces and maxima. POST/GET /registry/{agents|workflows|policies}; duplicate versions return 409. Submission resolves and snapshots definitions inside tenant scope.

Memory routes: PUT/GET/DELETE /memory/{namespace}/{id}, GET /memory/{namespace}?q=&limit=. Bound content to 16000 characters, enforce TTL, validate provenance and return only the authenticated tenant's records. Lexical memory search is labelled; it is not a semantic-memory model.

SSE uses durable event IDs and an authenticated reconnect cursor; it never places credentials in the URL. The UI keeps credentials in memory, does not persist them in localStorage, and renders untrusted content with textContent. Hooks verify HMAC over timestamp+raw body, reject stale/replayed messages, map configured hook identities to tenants, and reuse workflow submission validation/idempotency.

## 4. Bounded agent loop

A durable agent.run step checkpoints round, validated plan, tool cursor, observations and evaluation. Each tool result is checkpointed before moving to the next cursor. Crash recovery resumes committed progress. Tool effects remain at-least-once and must be idempotent; loop tools are read-only.

The evaluator checks substantive evidence/source references or finite arithmetic results. Failed evaluation triggers a bounded new query/strategy or validated model replan. Success, insufficient evidence, exhausted rounds, deadline or budget produce explicit distinct reasons. A worker polling loop does not count as an agent loop.

Remembered information has tenant/namespace/provenance, is bounded when recalled, and is treated as untrusted data. Recalled text cannot expand tool permissions or budgets.

## 5. Worker and model invariants

- Store.checkpoint and heartbeat require the current lease token and an active run.
- Heartbeat interval is below one third of the lease. Timeouts and cancellation stop the local coroutine; external side effects may still have occurred.
- Exception retries AND expired-lease recovery obey max_attempts. Terminal runs reject both success and failure updates.
- ModelGateway.complete preserves its existing call shape and accepts tenant/run_id/purpose metadata.
- Reserve conservative input+output budget before a provider request. Reconcile reported usage; preserve reservation after ambiguous billed failures. Never interpret absent usage as zero.
- Route fallback is limited to transport failures, 429 and transient 5xx; authentication/validation failures do not silently fall through.
- Prices are operator supplied; monetary totals are labelled estimates. No default price is invented.
- Tool registry metadata cannot bypass argument validation. Unknown tools, unapproved effects, cross-tenant memory and caller-selected remote endpoints fail closed.

## 6. Optional integration boundaries

Auth module contract: Authenticator(config).authenticate(header) -> Principal(tenant,subject,roles,scopes,attributes); require(principal,action,resource) enforces policy. Validate issuer, audience, expiry, subject, allowed algorithm and tenant claim for OIDC; no unverified JWT decoding.

Telemetry contract: span(name, attributes) is a no-op only when explicitly unconfigured; configured-but-unavailable instrumentation fails startup. No source bodies, prompts or secrets in attributes.

Persistence adapters need real service verification before the review ledger calls them verified. Local SQLite remains the default; a datastore connection string must never be silently ignored or downgraded.

An execution sandbox for arbitrary code is not activated by the current tools. Any added isolated execution adapter requires operator configuration, finite resource limits, no secrets and no implicit network access; otherwise expose unavailable status rather than pretend arithmetic AST validation is a sandbox.

## 7. Acceptance and self-review

AG-T01: existing DAG, tenant, idempotency, approval, fencing and MCP contracts remain valid.
AG-T02: real RAG HTTP + Agent API + independent worker completes a loop with recorded evaluation and at least one exercised refinement path.
AG-T03: resume a persisted checkpoint without losing completed observations; stale workers and cancelled runs cannot commit.
AG-T04: renew a long-running lease; repeatedly expire a lease and prove retry cap; cancel an in-flight task.
AG-T05: version immutability, tenant isolation, memory TTL, namespace/tool/budget policy denial and definition snapshots.
AG-T06: actual local provider HTTP fixture exercises routing/fallback/output limits/usage and budget rejection before a request; distinguish fixture evidence from a live paid provider.
AG-T07: real signed JWT validation, wrong issuer/audience/expiry/role/tenant rejection, separate approver identity, rate limit, HMAC tampering/replay and SSE reconnect.
AG-T08: optional configured service tests exercise their actual protocol; unavailable dependencies are not reported as successful.
AG-T09: review trace/event content for credential/source leakage and validate explicit completion/insufficient/budget/timeout outcomes.

Order: commit specification -> implement -> exercise meaningful unit/integration tests and real HTTP/MCP -> independent self-review -> fix findings -> publish evidence in docs/IMPLEMENTATION_REVIEW.md. Preserve any unresolved external dependencies with concrete prerequisites; do not rewrite acceptance criteria after a failure.
