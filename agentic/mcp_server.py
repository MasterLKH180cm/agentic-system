"""MCP stdio bridge for Codex and Claude Code clients (optional dependency)."""
import os

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise SystemExit("Install MCP support: pip install -e '.[mcp]'") from exc

mcp = FastMCP("agentic-workflows")
BASE_URL = os.getenv("AGENT_URL", "http://127.0.0.1:8001").rstrip("/")
API_KEY = os.getenv("AGENT_API_KEY", "local-agent-key")
HEADERS = {"Authorization": "Bearer " + API_KEY}


@mcp.tool()
def start_research_workflow(goal: str) -> dict:
    """Start an allowlisted, tenant-scoped workflow; does not approve side effects."""
    with httpx.Client(timeout=100) as client:
        response = client.post(BASE_URL + "/workflows", headers=HEADERS, json={"goal": goal})
        response.raise_for_status()
        return response.json()


@mcp.tool()
def get_workflow_status(workflow_id: str) -> dict:
    """Inspect a workflow, tool outputs, and citations."""
    with httpx.Client(timeout=20) as client:
        response = client.get(BASE_URL + "/workflows/" + workflow_id, headers=HEADERS)
        response.raise_for_status()
        return response.json()


@mcp.tool()
def get_workflow_events(workflow_id: str) -> list[dict]:
    """Inspect the append-only workflow execution events."""
    with httpx.Client(timeout=20) as client:
        response = client.get(
            BASE_URL + "/workflows/" + workflow_id + "/events", headers=HEADERS
        )
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    mcp.run(transport="stdio")
