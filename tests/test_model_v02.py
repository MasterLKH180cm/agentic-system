import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from agentic.api import create_app
from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.store import BudgetExceeded
from agentic.worker import Worker


HEADERS = {"Authorization": "Bearer test-user-key"}


@pytest.fixture
def providers(monkeypatch, tmp_path):
    state = {"calls": [], "primary_status": 503, "usage": True, "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append({"path": self.path, "payload": payload})
            if state["delay"]:
                time.sleep(state["delay"])
            status = state["primary_status"] if self.path.startswith("/primary/") else 200
            if status >= 400:
                body = {"error": {"message": "fixture failure"}}
            else:
                text = "Fixture response"
                message = payload["messages"][-1]["content"]
                if "allowed_tools" in message:
                    text = json.dumps({"steps": [{"id": "calculate", "tool": "math.calculate",
                                                  "args": {"expression": "6 * 7"}}]})
                if self.path.endswith("/messages"):
                    body = {"content": [{"type": "text", "text": text}]}
                    if state["usage"]:
                        body["usage"] = {"input_tokens": 7, "output_tokens": 11}
                else:
                    body = {"choices": [{"message": {"content": text}}]}
                    if state["usage"]:
                        body["usage"] = {"prompt_tokens": 7, "completion_tokens": 11}
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "model.db"))
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"test-user-key":"local"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"local":"local-rag-key"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"test-approver-key":"local"}')
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_TENANT_POLICIES_JSON", "{}")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    # The fallback-success case explicitly budgets both complete schema prompts.
    # Individual workflows retain their default or requested lower token ceiling.
    monkeypatch.setenv("AGENT_MAX_MODEL_TOKENS", "16000")
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "local-fixture-key")
    routes = [{"name": name, "provider": provider, "model": "fixture-model", "base_url": base + "/" + name,
               "api_key_env": "FIXTURE_MODEL_KEY", "purposes": ["planning", "synthesis"],
               "input_cost_per_million": 1.0, "output_cost_per_million": 2.0}
              for name, provider in (("primary", "openai"), ("secondary", "anthropic"))]
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps(routes))
    app = create_app(Settings.from_env())
    with TestClient(app) as client:
        yield client, app.state.store, app.state.model, app.state.tools, state
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def run_for(client, **limits):
    response = client.post("/workflows", headers=HEADERS, json={
        "goal": "Calculate 6 * 7", "steps": [{"id": "calc", "tool": "math.calculate", "args": {"expression": "6 * 7"}}],
        **limits,
    })
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def test_real_http_fallback_records_usage_and_output_caps(providers):
    client, store, model, _, state = providers
    run_id = run_for(client, max_cost_usd=0.01)
    answer = await model.complete("System", "Question", tenant="local", run_id=run_id)
    assert answer == "Fixture response"
    assert [c["path"] for c in state["calls"]] == ["/primary/chat/completions", "/secondary/messages"]
    assert state["calls"][0]["payload"]["max_completion_tokens"] == 1000
    assert state["calls"][1]["payload"]["max_tokens"] == 1000
    usage = store.usage("local", run_id)
    assert usage["calls"][0]["status"] == "uncertain_http_error"
    assert usage["calls"][0]["charged_tokens"] > 1000
    assert usage["calls"][1]["input_tokens"] == 7
    assert usage["calls"][1]["output_tokens"] == 11
    assert usage["estimated_cost_usd"] > 0
    assert not usage["usage_complete"]


async def test_budget_denial_happens_before_provider_request(providers):
    client, store, model, _, state = providers
    run_id = run_for(client, max_model_tokens=10)
    with pytest.raises(BudgetExceeded):
        await model.complete("System", "Question", tenant="local", run_id=run_id)
    assert state["calls"] == [] and store.usage("local", run_id)["calls"] == []


async def test_concurrent_reservations_cannot_overspend(providers):
    client, _, model, _, state = providers
    state.update(primary_status=200, delay=0.08)
    run_id = run_for(client, max_model_tokens=1500)
    results = await asyncio.gather(
        model.complete("System", "Question", tenant="local", run_id=run_id),
        model.complete("System", "Question", tenant="local", run_id=run_id),
        return_exceptions=True,
    )
    assert len(state["calls"]) == 1
    assert results.count("Fixture response") == 1
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1


async def test_authentication_failure_does_not_fallback(providers):
    client, _, model, _, state = providers
    state["primary_status"] = 401
    with pytest.raises(httpx.HTTPStatusError):
        await model.complete("System", "Question", tenant="local", run_id=run_for(client))
    assert len(state["calls"]) == 1


async def test_missing_usage_is_never_counted_as_zero(providers):
    client, store, model, _, state = providers
    state.update(primary_status=200, usage=False)
    run_id = run_for(client)
    await model.complete("System", "Question", tenant="local", run_id=run_id)
    usage = store.usage("local", run_id)
    assert usage["calls"][0]["status"] == "estimated_missing_usage"
    assert usage["charged_tokens"] > 1000 and not usage["usage_complete"]


async def test_cost_budget_requires_configured_prices(providers):
    client, _, model, _, state = providers
    model.config = replace(model.config, routes=tuple(
        replace(route, input_cost_per_million=None, output_cost_per_million=None) for route in model.config.routes
    ))
    with pytest.raises(BudgetExceeded):
        await model.complete("System", "Question", tenant="local", run_id=run_for(client, max_cost_usd=0.01))
    assert state["calls"] == []


@pytest.mark.parametrize("token_budget", [12000, 16000])
async def test_durable_planning_is_budgeted_and_idempotent(providers, token_budget):
    client, store, _, tools, state = providers
    payload = {"goal": "Calculate 6 * 7", "idempotency_key": "durable-plan", "max_model_tokens": token_budget}
    result = client.post("/workflows", headers=HEADERS, json=payload)
    assert result.status_code == 202
    run_id = result.json()["id"]
    assert state["calls"] == []
    assert store.get("local", run_id)["steps"][0]["tool"] == "workflow.plan"
    assert client.post("/workflows", headers=HEADERS, json=payload).json() == {"id": run_id, "duplicate": True}
    worker = Worker(store, tools)
    assert await worker.once() == 1
    if token_budget == 12000:
        assert await worker.once() == 0
        run = store.get("local", run_id)
        assert run["status"] == "failed" and len(run["steps"]) == 1
        assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions"]
        request = state["calls"][0]["payload"]
        reservation = (ModelGateway.input_units(request["messages"][0]["content"], request["messages"][1]["content"])
                       + request["max_completion_tokens"])
        usage = store.usage("local", run_id)
        assert usage["charged_tokens"] == reservation
        assert len(usage["calls"]) == 1 and usage["calls"][0]["status"] == "uncertain_http_error"
        assert reservation <= token_budget < 2 * reservation
        assert client.post("/workflows", headers=HEADERS, json=payload).json() == {"id": run_id, "duplicate": True}
        return
    assert await worker.once() == 1
    run = store.get("local", run_id)
    assert run["status"] == "completed" and run["steps"][1]["result"]["value"] == 42
    assert store.usage("local", run_id)["calls"][-1]["purpose"] == "planning"
    assert client.post("/workflows", headers=HEADERS, json=payload).json()["duplicate"]
    assert len(state["calls"]) == 2
