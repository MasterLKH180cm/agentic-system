# Agentic System v0.2 implementation review

Historical v0.2 record. Current evidence handling and verification are in the
[v0.3 review](IMPLEMENTATION_REVIEW_V03.md).

This ledger compares the implemented revision with [SPEC_V02.md](SPEC_V02.md),
which was committed before feature implementation. It separates source-backed
behavior, executed checks, protocol fixtures, and external verification still
required. A defined CI job is not a recorded passing CI run.

## Twelve-layer review

| Requirement | Implemented scope | Evidence and remaining boundary |
| --- | --- | --- |
| **AG-01 / L1 · Interface** | REST, minimal credential-in-memory UI, authenticated SSE with durable cursor, HMAC hooks with replay receipts | [Interface implementation](../agentic/interfaces.py); [platform checks](../tests/test_platform.py). Full interactive browser automation is not claimed. |
| **AG-02 / L2 · Identity & Gateway** | Strong production credential maps, separate approvers, verified JWT/JWKS, named permissions, tenant/namespace attributes, local or Redis rate limits | Signed JWTs use an actual local JWKS HTTP server; invalid claims and role/tenant denial are exercised. Actual Redis 7.4.12 shared-limit/expiry checks passed in CI. |
| **AG-03 / L3 · Control Plane** | Immutable agent/workflow/policy versions; policy intersection and resolved run snapshots | [Runtime tests](../tests/test_runtime_v02.py) check versions, snapshots, permitted namespaces, and denied tools. |
| **AG-04 / L4 · Orchestration** | Durable checkpoints/cursors, renewable leases, fencing, absolute deadlines, cancellation guards, capped exception and lease-expiry retries | Recovery, stale completion, cancellation, heartbeat, exhaustion, and persisted deadlines are exercised in runtime/workflow tests. This is not deterministic history replay. |
| **AG-05 / L5 · Agent Runtime** | Persisted plan/execute/evaluate/replan loop, bounded read-only tools, distinct stopping results | Refinement, successful arithmetic, insufficient evidence, checkpoint resume, and recovery after evaluation are tested. Lexical evidence acceptance is heuristic. |
| **AG-06 / L6 · Parallel Execution** | Dependency-aware DAG concurrency, finite active worker slots, tenant queue admission | Existing fan-out/fan-in tests remain. Loop substeps are sequential; there is no external queue, priority scheduler, or autoscaler. |
| **AG-07 / L7 · Model Gateway** | Purpose routing, transient fallback, atomic reservation, output limits, provider usage reconciliation, operator-priced cost estimates | [Model tests](../tests/test_model_v02.py) run actual local HTTP provider fixtures, including concurrent reservations. These are not paid-model calls or quality tests. |
| **AG-08 / L8 · Tool & Integration** | Typed arguments, execution-time policy checks, bounded fixed-endpoint RAG adapter, stdio MCP, approval-only artifacts | Unknown tools, unsafe arithmetic, cross-tenant retrieval, and role separation are checked. General code sandbox reports unavailable. |
| **AG-09 / L9 · Knowledge & Memory** | Namespace/tenant memory CRUD, expiry, validated run provenance, bounded lexical recall | Isolation, TTL, provenance, and protected run/artifact access are tested. Vector retrieval remains in the companion RAGs service. |
| **AG-10 / L10 · Persistence** | SQLite migration, PostgreSQL Store adapter, local/S3 artifacts, SQLite/Redis rate limits | Real PostgreSQL 16.2 Store and complete HTTP/MCP flows passed locally. CI passed actual PostgreSQL 16.16, Redis 7.4.12, and S3-compatible MinIO checks, plus the PostgreSQL HTTP/MCP flow. |
| **AG-11 / L11 · Governance** | Versioned policy maxima, distinct approval identity, same-JWT-subject self-approval denial, provider-secret checks, redacted event metadata | [Security tests](../tests/test_security.py), runtime tests, and platform tests. No managed secret vault or tamper-evident audit claim. |
| **AG-12 / L12 · Observability & Quality** | Run/step/model metadata spans, OTLP HTTP export, operation metrics, usage records, task stopping outcomes | Actual local OTLP HTTP receiver obtains decoded traces with forbidden content absent. Synthetic outcome checks do not establish broad answer quality. |

## Executed verification

| Verification | Recorded result | Scope |
| --- | --- | --- |
| Core regression suite | **35 passed** | Existing workflow/security tests plus runtime, evidence, and gateway tests |
| Request-body and cursor limits | **15 passed** | ASGI pre-parse size/total-deadline limits, no downstream execution on refusal, preserved disconnects, signed-64-bit event cursors |
| Complete ordinary local suite | **54 passed, 3 skipped** | The skipped tests require PostgreSQL, Redis and S3; PostgreSQL was exercised separately |
| Platform tests without optional storage services | **4 passed** | Signed JWT/JWKS, HMAC/replay/SSE/UI response, shared local limits/artifacts, actual OTLP HTTP export |
| PostgreSQL Store test | **1 passed on PostgreSQL 16.2** | Two Store instances, exclusive claim, heartbeat, checkpoint visibility, completion, stale-result refusal |
| Complete HTTP/MCP harness with SQLite | **Passed** | Actual RAG API, Agent API, independent worker, and both stdio MCP bridges |
| Complete HTTP/MCP harness with PostgreSQL 16.2 | **Passed** | The same process-boundary workflow using a real PostgreSQL store |
| CI ordinary suite and HTTP/MCP harness | **54 passed, 3 skipped; harness passed** | The three optional storage checks run in the separate external-service job |
| CI external-service platform suite | **7 passed, no skips** | Actual PostgreSQL 16.16, Redis 7.4.12, and MinIO, plus the four common platform checks |
| CI HTTP/MCP harness with PostgreSQL 16.16 | **Passed** | Actual APIs, independent worker, durable PostgreSQL state, and both stdio MCP bridges |
| Ruff and whitespace checks | **Passed** | `ruff check agentic tests --select E4,E7,E9,F`; `git diff --check` |
| UI packaging and JavaScript syntax | **Passed** | Built wheel contains `agentic/static/index.html`; extracted JavaScript passes `node --check` |
| README Mermaid syntax | **3 diagrams passed** | Mermaid 11.17.2 `parse` API with jsdom 26.1.0; an invalid control was rejected. Syntax validation only, without browser rendering. |
| Redis shared limit/expiry | **Passed against actual Redis 7.4.12 in CI** | Two clients share a bucket; exhaustion and positive bounded TTL are asserted |
| S3-compatible artifact isolation/integrity | **Passed against actual MinIO in CI** | Real bucket/object operations, cross-tenant denial, deliberate corruption rejection, and cleanup |
| Docker Compose full stack | **Passed in CI** | Actual builds, provider environment wiring, all three containers, external HTTP ingestion and completed workflow |
| Paid OpenAI/Anthropic calls; visual browser interaction | **Not executed** | Local protocol fixtures and served HTML checks do not verify these behaviors |

Recorded [CI run 44799515210](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44799515210)
passed all three jobs for agent commit `dfb4f7960ad685a29aa8ac79a45bd84eeb379ca1`,
with companion RAG implementation pinned to
`aff48b3a53029a2febea0366055845f14b69bbfa`. The ordinary and external-platform
counts overlap; they are not a combined unique-test count. The source-built
S3 fixture pins official MinIO commit
[`9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`](https://github.com/minio/minio/tree/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a)
(`RELEASE.2025-10-15T17-29-55Z`) and Go 1.24.8. This is an actual object-store
server, not a mocked S3 client. See the [workflow](../.github/workflows/ci.yml)
for disposable service setup and cleanup.

The complete harness checks six groups of behavior:

1. RAG token-aware ingestion, source offsets, hierarchy, tenant isolation, and
   atomic reindex with revision conflict detection.
2. Historical revisions and deletion/purge, plus typed structured aggregation.
3. Independent-worker DAG execution, idempotency, registry, successful arithmetic
   loop, evidence refinement until `rounds_exhausted`, and memory provenance.
4. Requester approval refusal (`403`), separate approval, persisted artifact value
   `42`, and an authenticated terminal SSE event.
5. A real HMAC-signed HTTP hook and replay refusal (`409`).
6. Actual `initialize`, `list_tools`, and `call_tool` exchanges with both stdio
   MCP servers, whose tools call the running HTTP services.

These tests use disposable stores and deterministic local providers. The model
gateway tests use actual local TCP HTTP servers with controlled provider
responses. They verify routing/accounting, not remote model quality.

## Reproduction

Install this repository with its development and relevant adapter extras, and
install the sibling RAGs package for process-boundary checks:

```bash
AGENT_ENV=test pytest -q
ruff check agentic tests --select E4,E7,E9,F
python tests/e2e_v02.py
```

The harness creates disposable stores, starts actual TCP RAG/Agent APIs and an
independent worker, then spawns both stdio MCP servers. It disposes its temporary
data and processes afterward. Set `AGENT_E2E_POSTGRES_URL` only to a disposable
database to exercise the same flow with PostgreSQL; the harness does not promise
to clear every row from an externally supplied database.

External platform tests require real services. Their skipped cases are not
passing integration evidence:

```bash
# Supply disposable endpoints and credentials in the process environment.
# AGENT_TEST_POSTGRES_URL, AGENT_TEST_REDIS_URL, AGENT_TEST_S3_ENDPOINT,
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
AGENT_ENV=test pytest -q tests/test_platform.py
```

The repository's `postgres`, `storage`, and `telemetry` extras supply the optional
clients; the `mcp` extra is required for the MCP gate. The baseline Compose file
does not start PostgreSQL, Redis, S3, or an OTLP collector.

## Acceptance mapping

| Gate | Evidence | Qualification |
| --- | --- | --- |
| **AG-T01** Existing DAG/tenant/idempotency/approval/fencing/MCP | `test_workflows.py`, `test_security.py`, both successful HTTP/MCP harness runs | Interactive Codex/Claude client applications were not launched |
| **AG-T02** Actual APIs/worker loop and refinement | `e2e_v02.py`; `test_loop_refines_and_records_evidence` | Real-process arithmetic succeeds; evidence exhaustion exercises refinement; a controlled retrieval test also verifies successful refinement |
| **AG-T03** Checkpoint resume and stale/cancelled denial | Crash-resume, post-evaluation resume, cancelled-mutation, and workflow fencing tests | Calls interrupted before checkpoint commit can still repeat |
| **AG-T04** Heartbeat, expiry caps, cancellation, deadlines | Long-running worker, repeated expiry, in-flight cancel, persisted deadline tests | Local coroutine cancellation cannot undo external effects |
| **AG-T05** Registry, policy, memory, provenance | Version/snapshot tests, namespace/tool policy tests, TTL and artifact-boundary tests | Memory recall is lexical |
| **AG-T06** Real local provider HTTP, fallback, usage, budgets | `test_model_v02.py` | Provider responses are controlled fixtures; no model-quality conclusion |
| **AG-T07** JWT, roles, hooks, SSE, rate limits | `test_platform.py` plus successful HTTP harness runs | Native UI is checked as served HTML, not an automated interactive sign-in flow |
| **AG-T08** Actual optional service protocols | PostgreSQL Store/full-process flow, Redis shared limits/expiry, and MinIO artifact isolation/integrity passed in the recorded CI run | Disposable single-service verification; no throughput, failover, or managed-cloud compatibility guarantee |
| **AG-T09** Redaction and explicit outcomes | Trace payload inspection, bounded tool output, evaluator/runtime tests | Protected run records intentionally retain evidence and checkpoints |

## Findings addressed during self-review

- **Recovery bounds:** lease expiration shares the attempt cap, and absolute
  step deadlines survive worker claims. Heartbeat/checkpoint/result mutations
  require a current lease and active run. Evaluation state is checkpointed so
  recovery does not accidentally consume an extra round.
- **Authorization:** run results, event streams, idempotent duplicates, memory,
  usage inspection, and artifact references retain their relevant tenant/namespace
  boundary. Artifact provenance records the producing run and namespace. The
  same JWT subject cannot request work and later approve that run.
- **Evidence handling:** RAG calls request `generate:false`; response size and
  citation content are bounded. Synthesis validates source references and packs
  complete JSON evidence rather than truncating serialized JSON mid-value.
- **Offline refinement:** refined searches remain on the available hybrid path.
  They do not silently select a fusion mode that requires an unconfigured
  generator.
- **Gateway accounting:** durable model planning belongs to the run ledger;
  concurrent calls reserve before sending. Missing/ambiguous usage remains
  estimated. A dollar budget requires operator prices; incomplete pricing
  returns a null estimate and a separate known-cost subtotal.
- **PostgreSQL precision:** epoch/lease columns use double precision so second
  resolution is retained. The adapter deliberately uses advisory-lock
  serialization and bound SQL parameters.
- **Operational metadata:** error events retain exception types without provider
  bodies. OTLP attributes exclude prompts, source text, credentials, and
  secret-bearing exception messages.
- **Ingress resource bounds:** request bodies are capped at 512,000 bytes before
  JSON parsing and must arrive within a single 30-second deadline. Oversize and
  timeout failures cannot invoke downstream handlers. REST/SSE cursors fit the
  database's signed 64-bit range. The full TCP/SSE/MCP harness passed again after
  this middleware was installed.
- **External-service fixture:** the initial MinIO registry image could not be
  downloaded, so the CI fixture now builds a fixed official source commit with
  its declared Go toolchain. The actual PostgreSQL/Redis/S3 tests and complete
  PostgreSQL HTTP/MCP workflow then passed; no mocked storage result substitutes
  for that service execution.

## Remaining boundaries

The gateway ledger covers agent planning and synthesis only. Retrieval requests
disable RAG answer generation, but RAG embeddings and model-assisted retrieval
can still incur costs outside it. UTF-8-byte reservation is a conservative
estimate, not exact provider tokenization, and operator-priced totals are not
an authoritative bill or whole-system hard spending cap.

The evidence evaluator uses lexical/source/numeric heuristics. Its accepted
result is not a semantic truth proof. Memory is lexical; adaptive substeps are
read-only and sequential. General code sandboxing, vector memory, an external
task queue, scheduling fairness, autoscaling, deterministic history replay,
managed secrets, and tamper-evident auditing are outside this implementation.

PostgreSQL shares durable state with serialized transactions; no `SKIP LOCKED`
queue or performance/HA guarantee is asserted. Redis is only a rate-limit
backend. S3 and Redis have passed their actual protocol tests in disposable CI
services; production capacity, resilience, and managed-service deployments still
need workload-specific verification. The supplied
Compose deployment remains a localhost development baseline; advanced settings
require explicit environment forwarding and optional extras.
