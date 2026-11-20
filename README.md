# Agentic System

[![CI](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/MasterLKH180cm/agentic-system/actions/workflows/ci.yml)

**Turn a goal into a validated workflow, execute it with independent workers, and retain the results and evidence.**

Agentic System is a Python 3.11+ / FastAPI workflow runtime. It handles planning,
dependencies, execution, approvals, and durable state; the companion
[RAGs](https://github.com/MasterLKH180cm/RAGs) service handles knowledge ingestion
and retrieval. The three-service stack runs locally without paid model credentials.

| Capability | What is implemented |
| --- | --- |
| Workflow planning | Deterministic offline plans or optional OpenAI / Anthropic plans, validated as directed acyclic graphs (DAGs) |
| Durable execution | SQLite workflow records, independent workers, leases, retries, and fencing against stale completions |
| Parallel steps | Ready steps run concurrently; dependent steps wait for completed prerequisites |
| Tenant boundaries | Requester tokens select tenants; each tenant has a distinct credential for RAGs |
| Human approval | Separate approver credentials; internal artifact publication always requires approval |
| Evidence and inspection | Tool results, citations, workflow status, and append-only execution events |
| MCP integration | A stdio bridge for starting and inspecting workflows from MCP clients |

Version 0.1 targets local development and small deployments. The original
twelve-layer design is **partially implemented**; see the
[coverage table](#12-layer-coverage) and [operational limits](#operational-limits).

## Contents

- [12-layer coverage](#12-layer-coverage)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Run a research workflow](#run-a-research-workflow)
- [Execution flow](#execution-flow)
- [Parallel steps and approval](#parallel-steps-and-approval)
- [Model configuration](#model-configuration)
- [Run with Python](#run-with-python)
- [MCP clients](#mcp-clients)
- [Validation](#validation)
- [Operational limits](#operational-limits)

## 12-layer coverage

The repository does not yet cover the full twelve-layer design. The table maps
working local foundations and missing requested components, and identifies
operational limitations separately from those functional gaps.

| Layer | Implemented in v0.1 | Remaining scope |
| --- | --- | --- |
| **L1 · Interface** | [REST API](agentic/api.py), generated API docs, polling | Web UI, SSE, webhooks |
| **L2 · Identity & Gateway** | [Bearer tokens](agentic/api.py) mapped to tenants; separate requester/approver credentials | OIDC, RBAC/ABAC, API gateway, rate limiting |
| **L3 · Control Plane** | [Workflow definitions and DAG validation](agentic/schemas.py), fixed tool allowlist | Agent registry, versioned workflow management, independent policy engine |
| **L4 · Orchestration** | [Persisted state machine](agentic/store.py), step results, leases and fencing | Distributed scheduling, durable history replay, finer-grained checkpoints |
| **L5 · Agent Runtime** | [Offline / LLM planner](agentic/planner.py), static plan execution | Iterative agent loop, replanning, outcome evaluator |
| **L6 · Parallel Execution** | [Worker batches](agentic/worker.py), SQLite pending queue, dependency fan-out/fan-in | Distributed queue and worker-pool management, fairness, backpressure |
| **L7 · Model Gateway** | [OpenAI / Anthropic HTTP clients](agentic/model.py), fixed provider configuration | Model routing, failover, rate limiting, token/cost budgets |
| **L8 · Tool & Integration** | [Allowlisted tool dispatch](agentic/tools.py), RAG API adapter, [MCP bridge](agentic/mcp_server.py) | Extensible tool registration, isolated execution sandbox |
| **L9 · Knowledge & Memory** | Tenant-scoped [RAG search with citations](agentic/tools.py); embeddings/vector retrieval in [RAGs](https://github.com/MasterLKH180cm/RAGs) | Agent working-memory management and long-term memory |
| **L10 · Persistence** | [SQLite WAL](agentic/store.py) for runs, step outputs, and events | PostgreSQL, object storage, Redis |
| **L11 · Governance** | Tenant checks, schema/tool bounds, human approval, execution events, environment-based secrets | Central guardrail policies, managed secrets, identity-backed approvals, tamper-evident audit |
| **L12 · Observability & Quality** | [Logs](agentic/worker.py), execution events, [integration / HTTP / MCP / Docker tests](.github/workflows/ci.yml) | OpenTelemetry, distributed tracing, task-quality evals, cost monitoring |

## Architecture

The API persists workflows. A separate worker process claims and executes ready
steps. API and worker share the workflow database; RAGs owns a separate index.

```mermaid
flowchart TD
    Client["HTTP client"] --> API["Agent API"]
    MCP["MCP client"] --> Bridge["stdio bridge"]
    Bridge --> API
    Approver["Human approver"] -->|Separate credential| API
    API -->|Validate and persist| DB[("Workflow database")]
    Worker["Independent worker"] -->|Claim steps and save results| DB
    Worker -->|Tenant credential| RAG["RAGs API"]
    RAG --> Index[("Knowledge index")]
    API -.->|Optional planning| Models["OpenAI or Anthropic"]
    Worker -.->|Optional synthesis| Models
```

In [Docker Compose](compose.yaml), `api`, `worker`, and `rags` are separate
containers. The API and worker use the same `agent-data` volume; RAGs uses
`rag-data`. Model services are optional external dependencies.

## Quick start

Requires Git and Docker with the Compose plugin. Clone the repositories as
siblings; the directory name `RAGs` is case-sensitive.

```bash
git clone https://github.com/MasterLKH180cm/RAGs.git
git clone https://github.com/MasterLKH180cm/agentic-system.git
cd agentic-system
docker compose up -d --build --wait
```

| Service | Local URL |
| --- | --- |
| Agent API documentation | <http://127.0.0.1:8001/docs> |
| RAG API documentation | <http://127.0.0.1:8000/docs> |
| Agent liveness | <http://127.0.0.1:8001/live> |
| RAG liveness | <http://127.0.0.1:8000/live> |

The default stack uses offline planning, local hashing embeddings, and extractive
answers. It binds API ports to localhost and uses development credentials.

```bash
docker compose logs -f api worker rags
# Stop services while keeping their named data volumes:
docker compose down
```

## Run a research workflow

These Bash examples use `curl` and the default development credentials.

**1. Index a document in RAGs.**

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/documents \
  -H 'Authorization: Bearer local-development-key' \
  -H 'Content-Type: application/json' \
  -d '{"document_id":"session-policy","title":"Session policy","source":"manual","text":"# Sessions\nSession timeout is 30 minutes."}'
```

**2. Submit a goal to the Agent API.**

```bash
curl --fail-with-body -sS http://127.0.0.1:8001/workflows \
  -H 'Authorization: Bearer local-agent-key' \
  -H 'Content-Type: application/json' \
  -d '{"goal":"What is the session timeout?","idempotency_key":"session-demo-001"}'
```

The API returns HTTP `202` with `id` and `duplicate` fields. Copy the returned
`id` into `WORKFLOW_ID`, then inspect the run until its `status` is `completed`.

```bash
WORKFLOW_ID=replace-with-returned-id
curl --fail-with-body -sS "http://127.0.0.1:8001/workflows/$WORKFLOW_ID" \
  -H 'Authorization: Bearer local-agent-key'

curl --fail-with-body -sS "http://127.0.0.1:8001/workflows/$WORKFLOW_ID/events" \
  -H 'Authorization: Bearer local-agent-key'
```

The offline plan runs `retrieve` followed by `synthesize`. Inspect
`steps[].result` for the answer and citations; the example should include
**30 minutes** and the retrieved document evidence. A repeated idempotency key
returns the existing run for an identical workflow; different content returns
HTTP `409`. Choose a new key when changing the workflow.

## Execution flow

Each step follows the scheduling flow below. The API validates tool names,
dependencies, and cycles before persisting the workflow.

```mermaid
flowchart TD
    Pending["Pending step"] --> Dependencies{"Dependencies completed?"}
    Dependencies -->|No| Pending
    Dependencies -->|Yes| Approval{"Approval still required?"}
    Approval -->|Yes| Waiting["Wait for separate approver"]
    Waiting -->|Approved| Pending
    Waiting -->|Rejected| Failed["Step failed"]
    Approval -->|No| Leased["Claim with lease and fencing token"]
    Leased --> Execute["Execute allowlisted tool"]
    Execute -->|Success| Complete["Save result and complete step"]
    Execute -->|Exception| Retry{"Attempts remaining?"}
    Retry -->|Yes, after backoff| Pending
    Retry -->|No| Failed
    Leased -.->|Lease expires and is reclaimed| Pending
```

A run begins as `queued`, then becomes `running`, `waiting_approval`,
`completed`, `failed`, or `cancelled` as its steps progress. Independent steps
can run concurrently; a failed step fails the run. Cancellation stops new
claims and prevents later successful results from being committed to that run.
`AGENT_MAX_PARALLEL` controls each worker's batch size (default `4`, range `1–16`).

| Tool | Behavior |
| --- | --- |
| `rag.search` | Queries the configured RAGs endpoint using the workflow tenant's credential |
| `model.summarize` | Synthesizes dependency outputs; offline mode combines evidence excerpts |
| `math.calculate` | Evaluates bounded arithmetic through an allowlisted AST |
| `data.merge` | Collects dependency outputs into one result |
| `artifact.publish` | Stores an approved artifact in the internal workflow record |

Explicit workflows support up to 24 steps. Model-generated plans support 1–8
steps and pass the same DAG and tool validation. The tools do not provide
arbitrary shell commands, SQL execution, or agent-selected network destinations.

## Parallel steps and approval

This workflow starts two independent calculations, merges their results, and
waits for approval before recording the artifact.

```mermaid
flowchart TD
    First["first: 7 × 6"] --> Join["join: merge results"]
    Second["second: 8 + 9"] --> Join
    Join --> Approval{"Separate approver"}
    Approval -->|Approve| Publish["publish: internal artifact"]
    Approval -->|Reject| Failed["Workflow failed"]
```

```bash
curl --fail-with-body -sS http://127.0.0.1:8001/workflows \
  -H 'Authorization: Bearer local-agent-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "goal":"Calculate two results and publish internally",
    "steps":[
      {"id":"first","tool":"math.calculate","args":{"expression":"7 * 6"}},
      {"id":"second","tool":"math.calculate","args":{"expression":"8 + 9"}},
      {"id":"join","tool":"data.merge","depends_on":["first","second"]},
      {"id":"publish","tool":"artifact.publish","depends_on":["join"]}
    ]
  }'
```

Set `WORKFLOW_ID` to this new run's `id`. Poll its status and inspect the
calculation results (`42` and `17`). Once `publish` is `waiting_approval`, use
the separate development approver token:

```bash
WORKFLOW_ID=replace-with-new-run-id
curl --fail-with-body -sS "http://127.0.0.1:8001/workflows/$WORKFLOW_ID/approve/publish" \
  -H 'Authorization: Bearer local-approver-key' \
  -H 'Content-Type: application/json' \
  -d '{"approved":true,"reason":"Reviewed calculation results"}'
```

`artifact.publish` always requires approval and writes only to the internal run
record. To reject, send `"approved":false`; to cancel an active run, send
`POST /workflows/{id}/cancel` with its requester credential.

## Model configuration

The three provider choices are independent:

| Setting | Default | Other supported values |
| --- | --- | --- |
| `AGENT_MODEL_PROVIDER` | `offline` | `openai`, `anthropic` for planning and synthesis |
| `RAGS_EMBEDDING_PROVIDER` | `local` | `openai` for trained embeddings |
| `RAGS_MODEL_PROVIDER` | `extractive` | `openai`, `anthropic` for RAG answer generation |

For Docker, copy [.env.example](.env.example) to `.env`, then edit it locally:

```bash
cp .env.example .env
```

For example, OpenAI embeddings and RAG answers with Anthropic planning/synthesis:

```dotenv
RAGS_EMBEDDING_PROVIDER=openai
RAGS_MODEL_PROVIDER=openai
AGENT_MODEL_PROVIDER=anthropic
OPENAI_API_KEY=replace-locally
ANTHROPIC_API_KEY=replace-locally
```

Optional model overrides are `OPENAI_EMBEDDING_MODEL`, `OPENAI_CHAT_MODEL`, and
`ANTHROPIC_CHAT_MODEL`; `OPENAI_BASE_URL` selects an OpenAI-compatible endpoint.
Compose forwards these settings to the relevant services. Apply changes with
`docker compose up -d --build --wait`. Re-ingest documents after changing the
embedding provider or model. Keep real credentials out of Git.

## Run with Python

Start RAGs using its [README](https://github.com/MasterLKH180cm/RAGs#quick-start).
In the `agentic-system` directory, create an environment once:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"
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
`python -m agentic.worker` in the other. API and worker must use the same
database file and model settings. Python processes read exported environment
variables; copying `.env` alone does not load them.

## MCP clients

Use the [Codex configuration](docs/codex-mcp.toml) or
[Claude Code configuration](docs/claude-mcp.json) as a local stdio configuration
example. Install `.[mcp]` in the Python environment used by the client, register
`python -m agentic.mcp_server`, and supply `AGENT_URL` and `AGENT_API_KEY` through
that client's local environment. Use the virtual environment's absolute Python
path if the client cannot resolve the installed package.

| MCP tool | Purpose |
| --- | --- |
| `start_research_workflow` | Submit a goal and return the run ID |
| `get_workflow_status` | Inspect status, results, and citations |
| `get_workflow_events` | Read execution events |

The API and worker must already be running. MCP exposes no approval tool.
Client choice is independent of the configured model provider;
[RAGs also provides a separate MCP bridge](https://github.com/MasterLKH180cm/RAGs).

## Validation

After installing the development extras:

```bash
AGENT_ENV=test pytest -q
ruff check agentic tests --select E4,E7,E9,F
```

[CI](.github/workflows/ci.yml) covers tenant isolation, DAG validation,
idempotency, approval, cancellation, lease recovery, and fencing. It also starts both real APIs
and a separate worker, exercises retrieval and synthesis over HTTP, performs
MCP `initialize` / `list_tools` / `call_tool` exchanges, and runs the full
three-container Docker stack. Provider configuration is checked with fixture
keys; these tests do not make paid model calls or launch interactive MCP clients.

## Operational limits

- **Storage:** SQLite WAL targets a shared host and local volume. Multi-node
  deployments need a suitable database and workflow/queue architecture.
- **Delivery:** Execution is at least once. Tool exceptions retry with backoff
  up to three attempts; expired leases are reclaimed separately. The current
  180-second lease has no heartbeat or separate cap on expiration recovery.
  Tools with external side effects must implement their own idempotency.
- **Cancellation:** In-flight network calls may finish; cancellation cannot
  undo side effects. Completion fencing protects the stored workflow results.
- **Identity:** Production startup requires `AGENT_KEYS_JSON` mapping strong
  requester tokens to tenants. Approvals use disjoint `AGENT_APPROVER_KEYS_JSON`
  credentials. `AGENT_RAG_KEYS_JSON` maps each tenant to a distinct RAGs token
  assigned to that same tenant in `RAGS_KEYS_JSON`.
- **Deployment:** The supplied Compose file explicitly runs in development
  mode. Public deployment additionally needs identity/role enforcement, TLS,
  quotas, backups, secret management, and operational monitoring.

Read [Architecture](docs/ARCHITECTURE.md) for security boundaries and migration
considerations. Contribution guidance is in [AGENTS.md](AGENTS.md) and
[CLAUDE.md](CLAUDE.md). Licensed under the [MIT License](LICENSE).
