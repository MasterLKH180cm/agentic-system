# Agentic System

[![CI](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml)

**Run durable workflows and bounded agents, inspect their evidence, and approve effects with a separate identity.**

Agentic System is a Python 3.11+ / FastAPI runtime for validated DAGs and
checkpointed **plan → execute → evaluate → replan** loops. Independent workers
execute typed tools; the companion [RAGs](https://github.com/MasterLKH180cm/RAGs)
service owns document ingestion and retrieval. The local stack works without
paid model credentials.

Version 0.2 adds immutable definitions, policy snapshots, memory, renewable
leases, model budgets, a small web UI, authenticated event streams, and optional
identity, persistence, and telemetry adapters. See the
[specification](docs/SPEC_V02.md) and
[verification ledger](docs/IMPLEMENTATION_REVIEW.md) for the implementation
contract, executed checks, and remaining boundaries.

## Contents

- [12-layer coverage](#12-layer-coverage)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Run a research workflow](#run-a-research-workflow)
- [Agent loops and memory](#agent-loops-and-memory)
- [Parallel steps and approval](#parallel-steps-and-approval)
- [Models and budgets](#models-and-budgets)
- [Deployment configuration](#deployment-configuration)
- [MCP clients](#mcp-clients)
- [Validation and limits](#validation-and-limits)

## 12-layer coverage

Each row maps the original design to executable behavior. Optional integrations
require their extras and configuration; implementation does not imply that every
external service or production topology has been verified.

| Layer | Implemented in v0.2 | Boundary |
| --- | --- | --- |
| **L1 · Interface** | [REST, web UI, resumable SSE, signed inbound hooks](agentic/interfaces.py) | UI accepts an existing credential; no interactive identity-provider sign-in |
| **L2 · Identity & Gateway** | [Tenant credentials, verified JWT/JWKS, roles, scopes, namespace checks, rate limits](agentic/auth.py) | Redis is an optional rate-limit backend; TLS and identity-provider operations remain deployment responsibilities |
| **L3 · Control Plane** | [Immutable agent/workflow/policy versions](agentic/control.py); resolved definitions and permissions snapshotted per run | Definitions can narrow operator policy, never grant additional authority |
| **L4 · Orchestration** | [Durable steps and loop checkpoints](agentic/store.py), fencing, heartbeats, retry caps, persisted deadlines, cancellation | At-least-once execution; no deterministic history-replay engine |
| **L5 · Agent Runtime** | [Bounded planning, observation, evaluation, and refinement](agentic/runtime.py) alongside static DAGs | Evidence evaluation is lexical and heuristic; arithmetic has a separate bounded verifier |
| **L6 · Parallel Execution** | [Dependency-aware workers](agentic/worker.py), bounded concurrency and tenant queue admission | Loop substeps execute sequentially; no external task queue, priority scheduler, or autoscaler |
| **L7 · Model Gateway** | [Purpose routes, transient fallback, reservations, usage reconciliation, output caps](agentic/model.py) | Gateway budgets cover agent planning/synthesis; RAGs and other external costs are separate |
| **L8 · Tool & Integration** | [Typed tool registry](agentic/tools.py), fixed RAG endpoint, [stdio MCP](agentic/mcp_server.py) | Adaptive loops use read-only tools; arbitrary-code sandbox is explicitly unavailable |
| **L9 · Knowledge & Memory** | [Tenant/namespace memory](agentic/memory.py), CRUD, TTL, provenance, bounded recall; cited retrieval through RAGs | Memory search is lexical; vector indexes belong to RAGs |
| **L10 · Persistence** | SQLite; [PostgreSQL adapter](agentic/postgres.py); [local/S3 artifacts](agentic/storage_adapters.py); optional Redis limits | PostgreSQL serializes transactions with an advisory lock; actual service tests do not establish throughput or high availability |
| **L11 · Governance** | Versioned tool/namespace/budget policies, disjoint approvers, provider-secret validation, redacted event metadata | Events are append-only application records, not a tamper-evident audit system |
| **L12 · Observability & Quality** | [Optional OTLP traces/metrics](agentic/telemetry.py), usage records, evaluator outcomes and regression tests | Protocol tests and synthetic outcomes do not establish general answer quality or paid-provider quality |

## Architecture

The API and workers share the workflow store. Planning runs in durable worker
steps, so submission does not perform an untracked model call. RAGs keeps its
own index and receives a distinct credential for each workflow tenant.

```mermaid
flowchart TD
    Client["Web UI or HTTP client"] --> API["Authenticated Agent API"]
    MCP["MCP client"] --> Bridge["stdio bridge"]
    Bridge --> API
    Hook["Signed webhook"] --> API
    API -->|Resolve policy and persist| Store[("Workflow store")]
    Worker["Independent workers"] <-->|Claim, checkpoint, commit| Store
    Worker --> Tools["Typed tools and runtime"]
    Tools --> RAG["Tenant-scoped RAGs API"]
    Tools --> Gateway["Model gateway"]
    Tools --> Artifacts["Approved artifact store"]
    Gateway --> Models["Configured model providers"]
    RAG --> Index[("Knowledge index")]
```

The supplied [Compose stack](compose.yaml) runs `api`, `worker`, and `rags` as
separate containers. SQLite files use the shared `agent-data` volume; RAGs uses
`rag-data`. [Architecture details](docs/ARCHITECTURE.md) explain recovery,
authorization, budgets, and optional infrastructure.

## Quick start

Requires Git and Docker with the Compose plugin. Clone the repositories as
siblings; `RAGs` is case-sensitive.

```bash
git clone https://github.com/MasterLKH180cm/RAGs.git
git clone https://github.com/MasterLKH180cm/agentic-system.git
cd agentic-system
docker compose up -d --build --wait
```

| Service | Local URL |
| --- | --- |
| Workflow UI | <http://127.0.0.1:8001/> |
| Agent API reference | <http://127.0.0.1:8001/docs> |
| RAG API reference | <http://127.0.0.1:8000/docs> |
| Public process liveness | <http://127.0.0.1:8001/live> and <http://127.0.0.1:8000/live> |

The default stack binds to localhost and uses offline planning, local hashing
embeddings, and extractive synthesis. Enter `local-agent-key` in the UI to submit
and inspect runs. Approval uses `local-approver-key`; RAGs uses
`local-development-key`. These are development credentials.

```bash
docker compose logs -f api worker rags
docker compose down  # Keep named data volumes.
```

## Run a research workflow

These Bash examples use `curl` and the default local credentials. First index
source evidence in RAGs, then submit a workflow:

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/documents \
  -H 'Authorization: Bearer local-development-key' \
  -H 'Content-Type: application/json' \
  -d '{"document_id":"session-policy","title":"Session policy","text":"# Sessions\nSession timeout is 30 minutes after inactivity."}'

curl --fail-with-body -sS http://127.0.0.1:8001/workflows \
  -H 'Authorization: Bearer local-agent-key' \
  -H 'Content-Type: application/json' \
  -d '{"goal":"What is the session timeout?","idempotency_key":"session-demo-001"}'
```

The second request returns HTTP `202` with `id` and `duplicate`. Copy `id` below.
The offline DAG retrieves evidence and synthesizes an answer with citations in
`steps[].result`; this example should include **30 minutes**.

```bash
RUN_ID=replace-with-returned-id
curl --fail-with-body -sS "http://127.0.0.1:8001/workflows/$RUN_ID" \
  -H 'Authorization: Bearer local-agent-key'

curl --fail-with-body -N "http://127.0.0.1:8001/workflows/$RUN_ID/events/stream" \
  -H 'Authorization: Bearer local-agent-key'
```

SSE sends durable event IDs and accepts `Last-Event-ID` or `?after=ID` on
reconnect. Connections end after 60 seconds or a terminal event; reconnect to
continue. Credentials stay in the Authorization header. JSON polling remains
available at `/workflows/{id}/events?after=ID`.

Request bodies are limited to 512,000 bytes before parsing and must arrive
within 30 seconds. The receive deadline does not limit workflow execution or
the subsequent SSE response.

An identical idempotency key and payload returns the existing run. Reusing the
key with different content returns `409`. Run states are `queued`, `running`,
`waiting_approval`, `completed`, `failed`, and `cancelled`.

## Agent loops and memory

Use `"runtime_mode":"agent_loop"` to checkpoint a plan, execute its read-only
tools, evaluate the observations, and refine within a finite round limit.
The default evidence evaluator checks known citation IDs, lexical relevance,
source overlap, and unsupported numbers. It is not a semantic truth verifier.

```mermaid
flowchart TD
    Start["New or recovered agent step"] --> Resume["Load checkpoint and policy snapshot"]
    Resume --> Plan["Plan or resume current round"]
    Plan --> Tool["Execute next permitted tool"]
    Tool --> Save["Checkpoint observation and cursor"]
    Save --> More{"Unfinished tools?"}
    More -->|Yes| Tool
    More -->|No| Evaluate["Evaluate evidence or arithmetic"]
    Evaluate --> Accepted{"Accepted?"}
    Accepted -->|Yes| Success["Checkpoint successful result"]
    Accepted -->|No| Limit{"Rounds remaining?"}
    Limit -->|Yes| Refine["Checkpoint feedback and refine"]
    Refine --> Plan
    Limit -->|No| Failed["Explicit insufficient-evidence result"]
```

For a deterministic example, register an immutable arithmetic agent and run it:

```bash
curl --fail-with-body -sS http://127.0.0.1:8001/registry/agents \
  -H 'Authorization: Bearer local-agent-key' -H 'Content-Type: application/json' \
  -d '{"name":"calculator","version":1,"allowed_tools":["math.calculate"],"evaluator":"arithmetic"}'

curl --fail-with-body -sS http://127.0.0.1:8001/workflows \
  -H 'Authorization: Bearer local-agent-key' -H 'Content-Type: application/json' \
  -d '{"goal":"Calculate 6 * 7","runtime_mode":"agent_loop","agent_id":"calculator","agent_version":1}'
```

The result is `42`, with `outcome`, `stop_reason`, `rounds`, and `evaluations`.
To try evidence refinement, submit a research goal with `runtime_mode` set to
`agent_loop` and `max_rounds` up to `5`. Exhausting rounds produces a failed run
with `outcome: insufficient_evidence`; budget and deadline stops have separate
outcomes. The default loop permits three rounds.

The registry also supports `/registry/workflows` and `/registry/policies`.
Versions are immutable, and each run stores its resolved definition and policy.
Memory is tenant- and namespace-bound; the default namespace is `default`:

```bash
curl --fail-with-body -sS http://127.0.0.1:8001/memory/default/session-note \
  -X PUT -H 'Authorization: Bearer local-agent-key' -H 'Content-Type: application/json' \
  -d '{"content":"Session timeout is 30 minutes after inactivity.","ttl_seconds":3600}'

curl --fail-with-body -sS 'http://127.0.0.1:8001/memory/default?q=session&limit=5' \
  -H 'Authorization: Bearer local-agent-key'
```

`GET` and `DELETE /memory/{namespace}/{id}` inspect or remove a record. Optional
`source_run_id` records validated run provenance. Expired records are excluded
from reads and recall. Search responses identify `search_type: lexical`.

## Parallel steps and approval

Explicit DAGs support up to 24 steps. Generated plans support up to 8. Ready
DAG steps can run concurrently; their dependants wait for completed results.

```mermaid
flowchart TD
    First["first: 7 × 6"] --> Join["join: merge results"]
    Second["second: 8 + 9"] --> Join
    Join --> Gate{"Separate approver"}
    Gate -->|Approve| Publish["Publish bounded JSON artifact"]
    Gate -->|Reject| Failed["Workflow failed"]
```

```bash
curl --fail-with-body -sS http://127.0.0.1:8001/workflows \
  -H 'Authorization: Bearer local-agent-key' -H 'Content-Type: application/json' \
  -d '{"goal":"Calculate and publish reviewed results","steps":[
    {"id":"first","tool":"math.calculate","args":{"expression":"7 * 6"}},
    {"id":"second","tool":"math.calculate","args":{"expression":"8 + 9"}},
    {"id":"join","tool":"data.merge","depends_on":["first","second"]},
    {"id":"publish","tool":"artifact.publish","depends_on":["join"]}
  ]}'
```

Copy the new run ID and inspect the calculations. When `publish` is
`waiting_approval`, approve with the separate credential:

```bash
RUN_ID=replace-with-new-run-id
curl --fail-with-body -sS "http://127.0.0.1:8001/workflows/$RUN_ID/approve/publish" \
  -H 'Authorization: Bearer local-approver-key' -H 'Content-Type: application/json' \
  -d '{"approved":true,"reason":"Reviewed calculation results"}'
```

Publication writes a content-addressed JSON artifact to local storage or the
configured S3 backend. Its `artifact_ref` identifies the authenticated
`GET /artifacts/{artifact_id}` endpoint. Publication always requires approval.
Send `approved:false` to reject, or `POST /workflows/{id}/cancel` with the
requester credential to cancel an active run.

`GET /tools` lists schemas for `rag.search`, `model.summarize`, `math.calculate`,
`data.merge`, `memory.search`, and `artifact.publish`. Arithmetic uses a bounded
AST interpreter. General shell, SQL, browser, and arbitrary-code execution are
not provided.

## Models and budgets

`AGENT_MODEL_PROVIDER=offline` is the default. `openai` and `anthropic` enable
planning and synthesis. Provider keys use environment variables or matching
`_FILE` variables; setting both forms is rejected.

For ordered routes, configure `AGENT_MODEL_ROUTES_JSON` in both API and worker
environments. For example:

```bash
export AGENT_MODEL_ROUTES_JSON='[
  {"name":"primary","provider":"openai","model":"gpt-4.1-mini","api_key_env":"OPENAI_API_KEY","purposes":["planning","synthesis"]},
  {"name":"fallback","provider":"anthropic","model":"claude-sonnet-4-5","api_key_env":"ANTHROPIC_API_KEY","purposes":["planning","synthesis"]}
]'
```

Supply those keys before starting the services. Routes are operator-configured;
requests cannot select their own provider URL. Fallback applies to transport
failures, HTTP `429`, and `5xx`, not authentication or validation errors.

Each gateway call reserves a conservative UTF-8-byte input estimate plus its
output allowance before sending a request. Reported provider usage reconciles
the reservation. Missing usage and ambiguous failures retain an estimated
charge. This estimate is not an exact model tokenizer.

Workflow `max_model_tokens` is bounded by server and policy maxima. To use
`max_cost_usd`, configure both `input_cost_per_million` and
`output_cost_per_million` on every applicable route. Prices are supplied by the
operator; monetary totals are estimates. Inspect `/usage?run_id=ID` or the run's
`usage`: incomplete pricing makes `estimated_cost_usd` null, while
`known_cost_usd`, `cost_complete`, and `usage_complete` identify partial totals.

**Budget scope:** the ledger covers this service's planning and synthesis calls.
`rag.search` requests retrieval with `generate:false`, but embedding and
model-assisted retrieval inside RAGs may still incur separate costs. There is
no aggregate billing cap across both services. The current evaluator is
deterministic and makes no model-evaluation call.

## Deployment configuration

### Python processes

Start RAGs using its [quick start](https://github.com/MasterLKH180cm/RAGs#quick-start).
In this repository, create an environment once:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,mcp]'
```

Run this setup in **both terminals**, from the same repository directory:

```bash
source .venv/bin/activate
export AGENT_ENV=development
export AGENT_API_KEY=local-agent-key
export AGENT_RAG_URL=http://127.0.0.1:8000
export AGENT_RAG_API_KEY=local-development-key
export AGENT_DB_PATH="$PWD/data/agentic.db"
```

Start `uvicorn agentic.api:app --host 127.0.0.1 --port 8001` in one terminal and
`python -m agentic.worker` in the other. Both processes must share persistence,
policy, model, and artifact configuration. Python reads exported environment
variables; it does not automatically load `.env`.

### Optional services and production identity

Install optional adapters with
`python -m pip install -e '.[postgres,storage,telemetry]'`.
[.env.example](.env.example) lists settings; apply them to the relevant processes.

| Settings | Purpose and boundary |
| --- | --- |
| `AGENT_KEYS_JSON`, `AGENT_APPROVER_KEYS_JSON` | Disjoint requester/approver token-to-tenant maps; production tokens require at least 24 characters |
| `AGENT_RAG_KEYS_JSON` | Tenant-to-RAG-token map; tokens must be distinct and mapped to the same tenants in RAGs |
| `AGENT_OIDC_ISSUER`, `AGENT_OIDC_AUDIENCE`, `AGENT_OIDC_JWKS_URL` | Configure together for verified JWTs; issuer, audience, expiry, algorithm, role, and tenant are checked |
| `AGENT_TENANT_POLICIES_JSON` | Operator maxima for tools, memory namespaces, steps, rounds, and model budgets |
| `AGENT_DATABASE_URL` | PostgreSQL DSN; otherwise `AGENT_DB_PATH` selects SQLite |
| `AGENT_REDIS_URL` | Shared rate-limit counters; otherwise a local SQLite limiter is used |
| `AGENT_ARTIFACT_BACKEND=s3`, `AGENT_S3_BUCKET` | S3 artifacts; optional `AGENT_S3_ENDPOINT` for a compatible service |
| `AGENT_TELEMETRY_ENABLED=1`, `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP HTTP traces/metrics with restricted metadata attributes |
| `AGENT_WEBHOOKS_JSON` | Configured hook identities, tenant bindings, and HMAC secrets |

JWT roles are `reader`, `requester`, and `approver`; requester and approver roles
cannot be combined. Optional scopes and memory-namespace attributes narrow
access. A JWT requester cannot approve their own run using the same subject.
The UI accepts a token already issued by the identity provider.

Hooks use `POST /hooks/workflows` with `X-Agent-Hook`, `X-Agent-Timestamp`, and
`X-Agent-Signature: sha256=HEX`. Sign `timestamp + "." + raw_body` using the
configured HMAC-SHA256 secret. Timestamps have a five-minute acceptance window;
replayed signatures return `409`. See [the protocol test](tests/e2e_v02.py) for
a complete signing example.

| Runtime bound | Default |
| --- | --- |
| `AGENT_MAX_PARALLEL` | 4 active steps per worker; configurable 1–16 |
| `AGENT_MAX_QUEUED_RUNS` | 100 active runs per tenant |
| `AGENT_LEASE_SECONDS` / `AGENT_HEARTBEAT_SECONDS` | 180 / 30 seconds; heartbeat must be below one third of the lease |
| `AGENT_STEP_TIMEOUT_SECONDS` / `AGENT_MAX_ATTEMPTS` | 300 seconds / 3 attempts, including expired leases |
| `AGENT_MAX_MODEL_TOKENS` / `AGENT_MAX_MODEL_OUTPUT_TOKENS` | 12,000 per run / 1,000 requested output tokens per call |
| `AGENT_RATE_PER_MINUTE` | 600 requests per tenant/subject fixed-minute bucket |

### Docker configuration

The Compose file is a local development baseline. `cp .env.example .env` lets
Compose read the basic provider/key switches it explicitly forwards, including
`RAGS_EMBEDDING_PROVIDER`, `RAGS_MODEL_PROVIDER`, and `AGENT_MODEL_PROVIDER`.
Apply changes with `docker compose up -d --build --wait`.

Advanced v0.2 variables require an explicit Compose override or deployment
environment; adding them to `.env` alone does not forward them to containers.
Optional image dependencies can be built with:

```bash
docker build --build-arg AGENT_EXTRAS=mcp,postgres,storage,telemetry \
  -t agentic-system:v0.2 .
```

The default image includes `mcp`. Public deployments also need TLS, appropriate
secret provisioning, backups, and operational monitoring. Keep development
credentials and real provider secrets out of Git.

## MCP clients

Use the [Codex](docs/codex-mcp.toml) or [Claude Code](docs/claude-mcp.json) local
configuration examples. Install `.[mcp]` in the client's Python environment and
register `python -m agentic.mcp_server` with `AGENT_URL` and `AGENT_API_KEY`.
Use the virtual environment's absolute Python path when necessary.

The bridge exposes `start_research_workflow`, `get_workflow_status`, and
`get_workflow_events`. API and worker must already be running. The submission
tool accepts a goal; advanced registry, memory, and loop configuration uses the
REST API. MCP exposes no approval tool.

## Validation and limits

```bash
AGENT_ENV=test pytest -q
ruff check agentic tests --select E4,E7,E9,F
# Install the sibling RAGs package and MCP extras first:
python tests/e2e_v02.py
```

The [recorded CI run](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44799515210)
passed all three jobs. The ordinary suite reports **54 passed, 3 skipped**
(35 core, 4 platform, and 15 request-body/cursor checks). The three optional
storage checks run in the external-service job, where **all 7 platform tests
passed without skips** against actual PostgreSQL 16.16, Redis 7.4.12, and a
MinIO server built from fixed upstream source. These counts overlap.

The full **RAG HTTP + Agent HTTP + independent worker + both stdio MCP bridges**
passed with SQLite and PostgreSQL. CI also built and exercised all three Docker
Compose containers. Local PostgreSQL 16.2 checks passed separately. The
[verification ledger](docs/IMPLEMENTATION_REVIEW.md) identifies the exact
commits, service fixture, assertions, and remaining boundaries. Paid model
quality and visual browser interaction have not been verified.

Execution is **at least once**. A crash between a tool response and its checkpoint
may repeat the call. Heartbeats, fencing, retry caps, and persisted deadlines
constrain recovery. Cancellation stops further claims and cancels local tasks
when detected; it cannot reverse an already completed external request.

SQLite targets processes sharing a local volume. PostgreSQL supports a shared
store with serialized advisory-lock transactions; throughput, fairness, and
high-availability behavior need workload-specific validation. Redis supplies
rate limits, not a task queue. No general code sandbox, vector-memory service,
or tamper-evident audit ledger is claimed.

Contribution rules are in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
Licensed under the [MIT License](LICENSE).
