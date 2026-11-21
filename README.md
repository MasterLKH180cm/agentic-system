# Agentic System

[![CI](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml)

**Run durable workflows and bounded agents, inspect their evidence, and approve effects with a separate identity.**

Agentic System is a Python 3.11+ / FastAPI runtime for validated DAGs and
checkpointed **plan → execute → evaluate → replan** loops. Independent workers
execute typed tools; the companion [RAGs](https://github.com/MasterLKH180cm/RAGs)
service owns document ingestion and retrieval. The local stack works without
paid model credentials.

Version 0.3 preserves evidence refusals across retrieval, synthesis, loops,
durable results, SSE and MCP, and snapshots the required RAG admission policy.
It builds on v0.2's immutable definitions, policy snapshots, memory, renewable
leases, model budgets, a small web UI, authenticated event streams, and optional
identity, persistence, and telemetry adapters. See the
[v0.3 specification](docs/SPEC_V03.md), [v0.2 specification](docs/SPEC_V02.md) and
[v0.3 verification ledger](docs/IMPLEMENTATION_REVIEW_V03.md) for the implementation
contract, executed checks, and remaining boundaries.

## Contents

- [12-layer coverage](#12-layer-coverage)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Run a research workflow](#run-a-research-workflow)
- [Agent loops and memory](#agent-loops-and-memory)
- [Evidence decisions and refusal](#evidence-decisions-and-refusal)
- [Parallel steps and approval](#parallel-steps-and-approval)
- [Models and budgets](#models-and-budgets)
- [Deployment configuration](#deployment-configuration)
- [MCP clients](#mcp-clients)
- [Validation and limits](#validation-and-limits)

## 12-layer coverage

Each row maps the original design to executable behavior. Optional integrations
require their extras and configuration; implementation does not imply that every
external service or production topology has been verified.

| Layer | Implemented behavior | Boundary |
| --- | --- | --- |
| **L1 · Interface** | [REST, web UI, resumable SSE, signed inbound hooks](agentic/interfaces.py) | UI accepts an existing credential; no interactive identity-provider sign-in |
| **L2 · Identity & Gateway** | [Tenant credentials, verified JWT/JWKS, roles, scopes, namespace checks, rate limits](agentic/auth.py) | Redis is an optional rate-limit backend; TLS and identity-provider operations remain deployment responsibilities |
| **L3 · Control Plane** | [Immutable agent/workflow/policy versions](agentic/control.py); definitions, permissions and RAG admission/profile allowlists snapshotted per run | Definitions can narrow operator policy, never grant additional authority |
| **L4 · Orchestration** | [Durable steps and loop checkpoints](agentic/store.py), fencing, heartbeats, retry caps, persisted deadlines, cancellation | At-least-once execution; no deterministic history-replay engine |
| **L5 · Agent Runtime** | [Bounded planning, observations, typed refusal, evaluation and refinement](agentic/runtime.py); committed observations survive recovery | Calibrated source admission bypasses only query-term overlap; final citation, number and source-support checks remain heuristic |
| **L6 · Parallel Execution** | [Dependency-aware workers](agentic/worker.py), bounded concurrency and tenant queue admission | Loop substeps execute sequentially; no external task queue, priority scheduler, or autoscaler |
| **L7 · Model Gateway** | [Purpose routes, transient fallback, reservations, usage reconciliation, full JSON prompt/output caps](agentic/model.py) | Gateway budgets cover agent planning/synthesis; RAGs and other external costs are separate |
| **L8 · Tool & Integration** | [Typed tool registry](agentic/tools.py), fixed RAG endpoint, [stdio MCP](agentic/mcp_server.py) | Adaptive loops use read-only tools; arbitrary-code sandbox is explicitly unavailable |
| **L9 · Knowledge & Memory** | [Tenant/namespace memory](agentic/memory.py), CRUD, TTL and provenance; typed RAG decisions, refused-source quarantine and exact structured zero | Memory search is lexical; vector indexes and calibrated model quality belong to RAGs |
| **L10 · Persistence** | SQLite; [PostgreSQL adapter](agentic/postgres.py); [local/S3 artifacts](agentic/storage_adapters.py); optional Redis limits | PostgreSQL serializes transactions with an advisory lock; actual service tests do not establish throughput or high availability |
| **L11 · Governance** | Versioned tool/namespace/budget policies, disjoint approvers, provider-secret validation, redacted event metadata | Events are append-only application records, not a tamper-evident audit system |
| **L12 · Observability & Quality** | [Optional OTLP traces/metrics](agentic/telemetry.py), usage records, durable refusal reasons and terminal REST/SSE/MCP inspection | Protocol tests and synthetic outcomes do not establish general answer quality or paid-provider quality |

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
The evidence evaluator checks admitted source identity, source overlap and
unsupported numbers. Valid calibrated admission can replace only its
question-to-source lexical-overlap check. Refused evidence stays quarantined;
neither a repeated citation ID nor an extractive answer can make it usable.
These checks are not a semantic truth verifier.

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

## Evidence decisions and refusal

The RAG query's domain outcome and the tool execution outcome are separate.
`rag.search` can execute successfully while returning
`retrieval.outcome:"insufficient_evidence"`. Another independent retrieval
branch can still supply admitted evidence. Refused observations contribute no
source text or citations to synthesis, including inside nested `data.merge`
results and recovered checkpoints.

```mermaid
flowchart TD
    Search["Authenticated retrieval branches"] --> Check["Validate outcomes, profiles and source IDs"]
    Check --> Accepted["Admitted evidence or exact values"]
    Check --> Refused["Quarantine refused branches"]
    Accepted --> Pack{"Usable evidence fits?"}
    Refused --> Pack
    Pack -->|No| Stop["Persist insufficient evidence; stop dependent work"]
    Pack -->|Yes| Synthesize["Extract evidence or validate model JSON"]
    Synthesize --> Decision{"Answer supported?"}
    Decision -->|No| Stop
    Decision -->|Yes| Complete["Persist answer and citations"]
    Stop --> Inspect["REST, SSE and MCP terminal inspection"]
    Complete --> Inspect
```

With no usable source or exact observation, synthesis refuses without making a
model call or usage reservation. Its outer `outcome` is
`insufficient_evidence`; the durable step/run becomes `failed`, and dependent
publication does not execute. A model can also explicitly refuse through its
validated JSON envelope. That refusal is a normal domain result, so it does
not trigger a generic missing-citation retry. Transport failures, malformed
model output, policy violations and evidence refusals remain distinguishable.

The fixed refusal reasons are `no_candidates`, `below_admission_threshold`,
`model_rejected` and `context_budget_exhausted`. Bounded agent loops record a
negative assessment and refine within their round limit. On exhaustion they
retain `outcome:insufficient_evidence`, `stop_reason:rounds_exhausted`, empty
citations and the last validated refusal reason. Recovery resumes after the
committed observation rather than retrieving again to reconstruct it.

| Operator policy | Behavior |
| --- | --- |
| `AGENT_RAG_ADMISSION_POLICY=compatibility` | Legacy evidence remains explicitly unassessed; an explicit refusal still takes precedence |
| `AGENT_RAG_ADMISSION_POLICY=calibrated_required` | Passage evidence must report a valid calibrated acceptance and an operator-allowlisted profile |
| `AGENT_RAG_ADMISSION_PROFILE_IDS_JSON` | Required mode needs 1–32 unique profile IDs; the resolved allowlist is snapshotted in each new run |

Calibrated bases are `calibrated_reranker` and `calibrated_answerability`.
`supported:true` alone cannot satisfy required mode. The companion RAG service
owns scorer artifacts, calibration and measured quality; selecting a profile
does not establish that it passed its evaluation. Exact structured aggregates
use a separate path with 1–20 validated dataset identities. A supported count
of zero is useful evidence; an absent dataset or fabricated provenance is not.
Finite arithmetic zero is also retained.

The current companion Qwen profile **failed its frozen quality gates and is not enabled by default**: it refused 9/16 held-out unanswerable cases, and admitted hybrid Recall@3 fell from 0.85 to 0.70. See the [measured RAG report](https://github.com/MasterLKH180cm/RAGs/blob/feat/spec-driven-v02/reports/admission-verification-qwen-v03.json). Correct refusal propagation and an allowlisted profile ID do not establish model quality.

Events expose only fixed outcome/reason/basis values, approved profile IDs,
bounded counts and validated engine identity. New diagnostic metadata does not
copy source text, questions, prompts or free-form upstream traces into events.

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
| `AGENT_RAG_ADMISSION_POLICY` | `compatibility` or `calibrated_required`, snapshotted for new workflows |
| `AGENT_RAG_ADMISSION_PROFILE_IDS_JSON` | Required-policy profile allowlist, resolved and snapshotted with the run |
| `AGENT_RAG_TIMEOUT_SECONDS` | Whole RAG HTTP exchange, default 180 seconds, finite range 1–360; durable worker deadlines still apply |
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

Advanced variables require an explicit Compose override or deployment
environment; adding them to `.env` alone does not forward them to containers.
Optional image dependencies can be built with:

```bash
docker build --build-arg AGENT_EXTRAS=mcp,postgres,storage,telemetry \
  -t agentic-system:v0.3 .
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

The current local core suite passed **121 unit tests and 147 integration tests**:
**268 unique cases, no selected skips**. Collection contains 273 cases; the
five external-service cases are explicitly deselected from the local core gate.
All **21 production Python modules** are included in coverage: **88.05% of
statements and 80.86% of branches**, with zero exclusions. The fixed minimums
are **85% line coverage and 75% branch coverage**, checked separately.
See the committed [test specification](docs/TEST_SPEC.md),
[measured results](docs/testing-results.json) and
[testing/code-quality review](docs/TEST_REVIEW.md) for scope and evidence.

Every collected pytest case must have exactly one primary layer: `unit`,
`integration` or `e2e`. The root [conftest.py](conftest.py) registers the options
before argument parsing and rejects unclassified/conflicting cases. JSON
inventories retain full node IDs, selected/deselected status and resource
markers; JUnit records layer and resource properties. `--require-no-skips`
fails selected skips, xfail/xpass and collection-time skips. A deliberately
deselected resource is recorded separately from an executed test.

```mermaid
flowchart TD
    Spec["Committed test specification"] --> Layers["Validate per-case test layers"]
    Layers --> Unit["Unit contracts"]
    Layers --> Integration["Local integration contracts"]
    Unit --> Coverage{"Lines at least 85%; branches at least 75%?"}
    Integration --> Coverage
    Spec --> Ready{"Real service and browser prerequisites ready?"}
    Ready -->|No| Fail["Fail the required gate"]
    Ready -->|Yes| Real["External storage and process/browser E2E"]
    Coverage -->|No| Fail
    Coverage -->|Yes| Review["Review inventories, reports and artifacts"]
    Real --> Review
```

### Run unit and integration gates

Run from this repository after checking out the tested RAGs companion as
`../RAGs`. CI pins the companion revision in its
[workflow](.github/workflows/ci.yml).

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
```

These commands combine the two parent-process suites. Independent workers,
stdio MCP, browser JavaScript and external-service execution have separate
evidence; their execution is not added to this Python coverage percentage.

### Run real end-to-end gates

The existing v0.2 and v0.3 HTTP/worker/MCP scripts passed again locally. The new
resilience harness passed **all six checks** with SQLite: actual worker loss
and lease recovery, in-flight cancellation fencing, SSE cursor reconnect,
transient retry, retry exhaustion and a persisted step deadline. It starts real
services with isolated fixture credentials, uses a fixed localhost fault proxy,
and checks terminal state over HTTP/MCP without editing workflow SQL state.

```bash
python tests/e2e_v02.py
python tests/e2e_v03.py
python tests/e2e_resilience.py --report reports/testing/resilience-sqlite.json

python -m playwright install --with-deps chromium
python tests/e2e_browser.py \
  --report reports/testing/browser.json \
  --artifacts reports/testing/browser-artifacts
```

The browser gate drives eight checks against actual APIs and an independent
worker: success, refusal, authorization, separate approval, stream ownership,
stale status/submission responses and credential lifetime. Missing Chromium
fails the gate. The actual [Chromium CI job](https://github.com/MasterLKH180cm/agentic-system/actions/runs/44882624007/job/136781024206)
passed **8/8 checks**, including four captured screenshots and clean service/browser
shutdown. This workspace's local browser attempt was blocked by UNIX-socket
restrictions and remains recorded as **failed, 0/8**; it is not counted as an
executed browser gate. Inspect the exact PR revision's checks for the final
remote outcome.

For the five actual PostgreSQL/Redis/S3 cases, install `.[postgres,storage]`,
provision disposable services, and configure `AGENT_TEST_POSTGRES_URL`,
`AGENT_TEST_REDIS_URL`, `AGENT_TEST_S3_ENDPOINT` and the fixture AWS credentials
shown in CI. Then run:

```bash
python -m pytest -q -m "integration and external" --require-no-skips \
  --test-inventory reports/testing/inventory-storage.json \
  --junitxml reports/testing/junit-storage.xml

# Set this to a separately created disposable database for this harness.
AGENT_E2E_POSTGRES_URL="${AGENT_E2E_POSTGRES_URL:?Set the disposable recovery database URL}" \
  python tests/e2e_resilience.py --report reports/testing/resilience-postgres.json
```

CI provisions separate PostgreSQL databases for v0.2, v0.3 and resilience,
retains all three Compose containers, and retains actual RAPTOR/LightRAG and
frozen-profile admission chains. Review the exact PR revision's checks and artifacts for the remote outcome.
Earlier execution records are retained in the
[v0.3 ledger](docs/IMPLEMENTATION_REVIEW_V03.md); they do not replace current
revision checks or the frozen RAG quality benchmark.

This iteration also corrects concurrent usage settlement, boolean/oversized
price validation, duplicated usage parsing, SQLite connection cleanup and
browser asynchronous ownership. PostgreSQL's four model-usage accounting
columns now use `BIGINT`, with a targeted migration for existing stores. The
[review](docs/TEST_REVIEW.md#corrections-and-regression-evidence) maps each
correction to its regression tests and identifies required external evidence.

### Execution and quality boundaries

Execution is **at least once**. A crash between a tool response and its checkpoint
may repeat the call. Heartbeats, fencing, retry caps, and persisted deadlines
constrain recovery. Cancellation stops further claims and cancels local tasks
when detected; it cannot reverse an already completed external request.

SQLite targets processes sharing a local volume. PostgreSQL supports a shared
store with serialized advisory-lock transactions; throughput, fairness, and
high-availability behavior need workload-specific validation. Redis supplies
rate limits, not a task queue. No general code sandbox, vector-memory service,
or tamper-evident audit ledger is claimed.

The companion Qwen profile's frozen quality gates remain failed: **9/16**
held-out unanswerable cases refused, with admitted hybrid Recall@3 falling from
**0.85 to 0.70**. The profile remains disabled by default. Agent refusal,
accounting and browser tests do not change those measurements. Citation and
numeric support checks remain heuristic; paid-provider answer quality and
production throughput/failover are outside these functional test results.

Contribution rules are in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
Licensed under the [MIT License](LICENSE).
