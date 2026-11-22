"""Real MCP registration/calls against an authenticated local HTTP fixture."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from mcp.server.fastmcp.exceptions import ToolError
import pytest

from agentic.features import FeatureDisabled, Features
from agentic import mcp_server

pytestmark = pytest.mark.integration


@pytest.fixture
def bridge(monkeypatch):
    state = {"calls": [], "features": Features().effective(), "provider": "offline"}

    class Handler(BaseHTTPRequestHandler):
        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["calls"].append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
            if self.path == "/capabilities":
                self.respond(state.get("capabilities", {"features": state["features"],
                                                         "model": {"provider": state["provider"]}}))
            else:
                self.respond([] if self.path.endswith("/events") else {"status": "completed"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
            self.respond({"id": "c009f6e0-dbae-4ef1-8fcb-547e806f7a96", "duplicate": False})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(mcp_server, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(mcp_server, "HEADERS", {"Authorization": "Bearer local-fixture-key", "X-Agent-Interface": "mcp"})
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


async def test_bridge_enabled_tools_forward_credentials_and_server_marker(bridge):
    server = mcp_server.create_server(Features())
    assert {tool.name for tool in await server.list_tools()} == {
        "start_research_workflow", "get_workflow_status", "get_workflow_events",
    }
    await server.call_tool("start_research_workflow", {"goal": "calculate 6 * 7"})
    await server.call_tool("get_workflow_status", {"workflow_id": "fixture-id"})
    await server.call_tool("get_workflow_events", {"workflow_id": "fixture-id"})
    assert len([call for call in bridge["calls"] if call["path"] == "/capabilities"]) == 4
    post = [call for call in bridge["calls"] if call["method"] == "POST"]
    assert len(post) == 1 and post[0]["body"] == {"goal": "calculate 6 * 7"}
    assert bridge["calls"][-1]["path"] == "/workflows/fixture-id/events"
    assert all(call["headers"]["Authorization"] == "Bearer local-fixture-key" for call in bridge["calls"])
    assert all(call["headers"]["X-Agent-Interface"] == "mcp" for call in bridge["calls"])


def test_local_mcp_disable_never_contacts_service(bridge):
    with pytest.raises(FeatureDisabled, match="mcp"):
        mcp_server.create_server(Features({"mcp": False}))
    assert bridge["calls"] == []
    bridge["features"]["mcp"] = False
    with pytest.raises(FeatureDisabled, match="mcp"):
        mcp_server.create_server(Features())
    assert [call["path"] for call in bridge["calls"]] == ["/capabilities"]


@pytest.mark.parametrize("local,remote,provider", [
    ({"dag": False}, {}, "offline"), ({}, {"dag": False}, "offline"),
    ({"model_generation": False}, {}, "openai"), ({}, {"model_generation": False}, "routed"),
])
async def test_disabled_submission_is_absent_from_mcp_tools(bridge, local, remote, provider):
    bridge["features"].update(remote)
    bridge["provider"] = provider
    server = mcp_server.create_server(Features(local))
    assert {tool.name for tool in await server.list_tools()} == {"get_workflow_status", "get_workflow_events"}


@pytest.mark.parametrize("feature,tool,args", [
    ("mcp", "get_workflow_status", {"workflow_id": "fixture-id"}),
    ("dag", "start_research_workflow", {"goal": "calculate 6 * 7"}),
    ("model_generation", "start_research_workflow", {"goal": "calculate 6 * 7"}),
])
async def test_remote_disable_after_registration_blocks_work(bridge, feature, tool, args):
    bridge["provider"] = "openai"
    server = mcp_server.create_server(Features())
    bridge["features"][feature] = False
    with pytest.raises(ToolError, match="Feature disabled: " + feature):
        await server.call_tool(tool, args)
    assert all(call["path"] == "/capabilities" for call in bridge["calls"])


@pytest.mark.parametrize("malformed", [
    [], {}, {"features": {}, "model": {"provider": "offline"}},
    {"features": {"mcp": True, "dag": True, "model_generation": 1}, "model": {"provider": "offline"}},
    {"features": Features().effective(), "model": {"provider": "private-invalid-provider"}},
])
def test_invalid_remote_capabilities_fail_closed(bridge, malformed):
    bridge["capabilities"] = malformed
    with pytest.raises(ValueError) as error:
        mcp_server.create_server(Features())
    assert "private-invalid-provider" not in str(error.value)
    assert all(call["path"] == "/capabilities" for call in bridge["calls"])
