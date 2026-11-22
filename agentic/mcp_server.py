"""MCP stdio bridge with local and authenticated remote feature permissions."""
import os
from urllib.parse import quote

import httpx

from agentic.features import Features


BASE_URL = os.getenv("AGENT_URL", "http://127.0.0.1:8001").rstrip("/")
API_KEY = os.getenv("AGENT_API_KEY", "local-agent-key")
HEADERS = {"Authorization": "Bearer " + API_KEY, "X-Agent-Interface": "mcp"}


def remote_capabilities() -> dict:
    with httpx.Client(timeout=20) as client:
        response = client.get(BASE_URL + "/capabilities", headers=HEADERS)
        response.raise_for_status()
        result = response.json()
    if (not isinstance(result, dict) or not isinstance(result.get("features"), dict)
            or not isinstance(result.get("model"), dict)
            or result["model"].get("provider") not in {"offline", "openai", "anthropic", "routed"}):
        raise ValueError("Agent server returned invalid capabilities")
    # Do not interpret an older/malformed capability response as permission.
    if not {"mcp", "dag", "model_generation"} <= result["features"].keys():
        raise ValueError("Agent server returned incomplete feature permissions")
    Features(result["features"])
    return result


def permissions(local: Features, remote: dict) -> Features:
    effective = Features(local.effective(remote["features"]))
    effective.require("mcp")
    return effective


def create_server(features: Features | None = None):
    local = features if features is not None else Features.from_env()
    local.require("mcp")
    remote = remote_capabilities()
    effective = permissions(local, remote)
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support: pip install -e '.[mcp]'") from exc
    server = FastMCP("agentic-workflows")

    def checked(*, submitting=False):
        current = remote_capabilities()
        allowed = permissions(local, current)
        if submitting:
            allowed.require("dag")
            if current["model"]["provider"] != "offline":
                allowed.require("model_generation")

    if effective.enabled("dag") and (remote["model"]["provider"] == "offline"
                                      or effective.enabled("model_generation")):
        @server.tool()
        def start_research_workflow(goal: str) -> dict:
            """Start an allowlisted, tenant-scoped workflow; does not approve side effects."""
            checked(submitting=True)
            with httpx.Client(timeout=100) as client:
                response = client.post(BASE_URL + "/workflows", headers=HEADERS, json={"goal": goal})
                response.raise_for_status()
                return response.json()

    @server.tool()
    def get_workflow_status(workflow_id: str) -> dict:
        """Inspect a workflow, tool outputs, and citations."""
        checked()
        with httpx.Client(timeout=20) as client:
            response = client.get(BASE_URL + "/workflows/" + quote(workflow_id, safe=""), headers=HEADERS)
            response.raise_for_status()
            return response.json()

    @server.tool()
    def get_workflow_events(workflow_id: str) -> list[dict]:
        """Inspect the append-only workflow execution events."""
        checked()
        with httpx.Client(timeout=20) as client:
            response = client.get(BASE_URL + "/workflows/" + quote(workflow_id, safe="") + "/events", headers=HEADERS)
            response.raise_for_status()
            return response.json()

    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
