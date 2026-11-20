# Agentic System — durable workflow execution over RAGs

This repository is the execution plane; [RAGs](https://github.com/MasterLKH180cm/RAGs)
is a separately deployable knowledge/retrieval plane.

Features: validated DAGs, tenant-isolated workflow storage, durable checkpoints,
independent leased workers, parallel ready-step execution, at-least-once retry,
lease fencing, append-only events, cancellation, manual approval using **separate**
credentials, bounded tools, MCP for Codex and Claude Code, and optional OpenAI /
Anthropic planning and synthesis.

> v0.1 is a verified, runnable foundation, NOT a Temporal replacement or
> unbounded autonomous agent. The exact operational limits are documented.

## Full stack with Docker

Clone both repositories as siblings (case-sensitive RAGs directory):

    git clone https://github.com/MasterLKH180cm/RAGs.git
    git clone https://github.com/MasterLKH180cm/agentic-system.git
    cd agentic-system
    docker compose up --build

RAG API: http://127.0.0.1:8000/docs
Agent API: http://127.0.0.1:8001/docs

This local Compose stack is restricted to localhost and uses DEMO credentials.
Do not expose it on the public internet.

## Without Docker

Start RAGs as documented in its README. In agentic-system:

    python -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev,mcp]"
    export AGENT_ENV=development
    export AGENT_API_KEY=local-agent-key
    export AGENT_RAG_URL=http://127.0.0.1:8000
    export AGENT_RAG_API_KEY=local-development-key

In one terminal:

    uvicorn agentic.api:app --port 8001

In another terminal, with the same environment:

    python -m agentic.worker

The API and worker must share the same AGENT_DB_PATH (SQLite file).

## Example: RAG-backed agent workflow

Index a knowledge document in RAGs:

    curl -X POST http://127.0.0.1:8000/documents \
      -H 'Authorization: Bearer local-development-key' \
      -H 'Content-Type: application/json' \
      -d '{"title":"Session policy","text":"# Sessions\nSession timeout is 30 minutes."}'

Submit:

    curl -X POST http://127.0.0.1:8001/workflows \
      -H 'Authorization: Bearer local-agent-key' \
      -H 'Content-Type: application/json' \
      -d '{"goal":"What is the session timeout?","idempotency_key":"sample-001"}'

The service returns a durable workflow UUID. Poll:

    curl http://127.0.0.1:8001/workflows/WORKFLOW_ID \
      -H 'Authorization: Bearer local-agent-key'

Default offline planning creates a retrieve step followed by synthesis.
With AGENT_MODEL_PROVIDER=openai or anthropic, the planner proposes a JSON DAG
that is **validated and limited** to the same allowlisted tools. Independent
steps execute concurrently; dependent steps wait for prerequisites.

## Example: parallel steps and approval

Submit explicit steps using the same POST /workflows:

    {
      "goal":"Calculate two results and publish internally",
      "steps":[
        {"id":"first","tool":"math.calculate","args":{"expression":"7 * 6"}},
        {"id":"second","tool":"math.calculate","args":{"expression":"8 + 9"}},
        {"id":"join","tool":"data.merge","depends_on":["first","second"]},
        {"id":"publish","tool":"artifact.publish","depends_on":["join"]}
      ]
    }

The publish step waits for a separately authenticated approval:

    curl -X POST http://127.0.0.1:8001/workflows/WORKFLOW_ID/approve/publish \
      -H 'Authorization: Bearer local-approver-key' \
      -H 'Content-Type: application/json' \
      -d '{"approved":true,"reason":"Reviewed output"}'

The publish tool only writes the artifact to the internal workflow record.
Production approval keys are supplied via AGENT_APPROVER_KEYS_JSON, mapped
to tenant IDs, and are distinct from requester tokens.

## Codex / Claude Code

Both clients can connect through the stdio MCP tools:

    AGENT_URL=http://127.0.0.1:8001 \
      AGENT_API_KEY=local-agent-key \
      python -m agentic.mcp_server

In the client's local MCP configuration, register command "python", args
["-m","agentic.mcp_server"], and environment variables AGENT_URL / AGENT_API_KEY
from a local secret store. MCP tools: start_research_workflow,
get_workflow_status, get_workflow_events. The RAGs repo exposes its own MCP
read-only knowledge search. AGENTS.md and CLAUDE.md align coding instructions.

## Testing and deployment considerations

    AGENT_ENV=test pytest -q
    ruff check agentic tests --select E4,E7,E9,F

CI also launches **both actual APIs and a real worker process** to run an
end-to-end RAG retrieval and dependent synthesis workflow.

Production requires AGENT_KEYS_JSON (strong per-tenant tokens) and an external
identity gateway; approvals additionally require AGENT_APPROVER_KEYS_JSON.
See [Architecture](docs/ARCHITECTURE.md) for threat model and limitations.
