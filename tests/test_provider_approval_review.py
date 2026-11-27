"""Provider configuration, strict JSON and effectful approval regression contracts."""
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from agentic.api import create_app
from agentic.config import ModelRoute, Settings
from agentic.evidence import strict_json_loads
from agentic.model import response_text
from agentic.schemas import ApprovalIn, Step, WorkflowIn
from agentic.store import Store
from agentic.tools import Tools
from test_evidence_v03 import reply as rag_reply
from test_model_failures import provider as provider


@pytest.mark.unit
@pytest.mark.parametrize("changes", [
    {"input_cost_per_million": float("nan")}, {"output_cost_per_million": float("inf")},
    {"input_cost_per_million": -1}, {"input_cost_per_million": True},
    {"input_cost_per_million": "0.1"}, {"input_cost_per_million": 10**400},
    {"provider": "unregistered"}, {"provider": []}, {"name": " "}, {"name": 42},
    {"model": ""}, {"model": "x" * 201}, {"purposes": ("synthesis", "synthesis")},
    {"purposes": ("unsafe",)}, {"purposes": (["synthesis"],)}, {"purposes": ()},
])
def test_direct_route_construction_enforces_the_same_contract_as_environment(changes):
    valid = dict(name="fixture", provider="openai", model="fixture-model",
                 base_url="http://127.0.0.1/v1", api_key="fixture")
    with pytest.raises(ValueError):
        ModelRoute(**{**valid, **changes})


@pytest.mark.unit
@pytest.mark.parametrize("raw", [
    '{"n":1e309}', '{"n":-1e309}', '{"n":NaN}', '{"n":Infinity}',
    '{"n":1,"n":2}', '{"n":"\\ud800"}', '{"\\udfff":0}',
    "[" * 65 + "0" + "]" * 65, "[" * 1500 + "]" * 1500,
])
def test_shared_json_contract_rejects_nonfinite_unicode_and_ambiguous_values(raw):
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.unit
def test_shared_json_contract_preserves_valid_unicode_and_scalar_types():
    assert strict_json_loads('{"text":"中文 🧠","n":1.25,"items":[0,false,null]}') == {
        "text": "中文 🧠", "n": 1.25, "items": [0, False, None],
    }


@pytest.mark.unit
@pytest.mark.parametrize("provider,body", [
    ("openai", {}), ("openai", {"choices": []}), ("openai", {"choices": [None]}),
    ("openai", {"choices": [{"message": None}]}),
    ("openai", {"choices": [{"message": {"content": "answer"}}] * 2}),
    ("anthropic", {"content": None}), ("anthropic", {"content": []}),
    ("anthropic", {"content": [{"type": "text", "text": "answer"}], "stop_reason": []}),
    ("anthropic", {"content": [{"type": "thinking", "thinking": "synthetic private reasoning"}]}),
])
def test_malformed_content_contracts_fail_with_controlled_errors(provider, body):
    with pytest.raises(ValueError):
        response_text(provider, body)


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, "true", "false", "yes", "no", None, [], {}])
def test_approval_contracts_require_explicit_booleans(value):
    with pytest.raises(ValidationError):
        ApprovalIn(approved=value)
    with pytest.raises(ValidationError):
        Step(id="calculate", tool="math.calculate", args={"expression": "2+3"}, approval_required=value)


def awaiting_store(path):
    store = Store(str(path))
    run = store.submit("tenant", WorkflowIn(goal="Publish with a human decision", steps=[
        Step(id="publish", tool="artifact.publish"),
    ]))["id"]
    assert store.claim() is None
    assert store.get("tenant", run)["steps"][0]["status"] == "waiting_approval"
    return store, run


@pytest.mark.integration
@pytest.mark.parametrize("value", [0, 1, 1.0, "true", "false", None, [], {}])
def test_storage_rejects_coerced_approval_before_mutating_state(tmp_path, value):
    store, run = awaiting_store(tmp_path / "approval.db")
    before, events = store.get("tenant", run), store.events("tenant", run)
    with pytest.raises(ValueError, match="boolean"):
        store.approve("tenant", run, "publish", value, "")
    assert store.get("tenant", run) == before
    assert store.events("tenant", run) == events
    assert store.claim() is None


@pytest.mark.integration
@pytest.mark.parametrize("value", [True, False])
def test_storage_preserves_explicit_approval_and_rejection(tmp_path, value):
    store, run = awaiting_store(tmp_path / "approval.db")
    assert store.approve("tenant", run, "publish", value, "Reviewed by separate approver")
    state = store.get("tenant", run)
    assert state["steps"][0]["approved"] is value
    assert (store.claim() is not None) is value


@pytest.mark.integration
@pytest.mark.parametrize("value", [0, 1, "true", "false", "yes"])
def test_http_malformed_approval_does_not_release_pending_effect(tmp_path, monkeypatch, value):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"requester":"tenant"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"approver":"tenant"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"tenant":"rag-key"}')
    monkeypatch.setenv("AGENT_RATE_DB_PATH", str(tmp_path / "limits.db"))
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_WEBHOOKS_JSON", "{}")
    for name in ("AGENT_DATABASE_URL", "AGENT_MODEL_ROUTES_JSON", "AGENT_REDIS_URL",
                 "AGENT_OIDC_ISSUER", "AGENT_OIDC_AUDIENCE", "AGENT_OIDC_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = replace(Settings.from_env(), db_path=str(tmp_path / "workflows.db"))
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/workflows", headers={"Authorization": "Bearer requester"}, json={
            "goal": "Publish with a human decision", "steps": [{"id": "publish", "tool": "artifact.publish"}],
        })
        assert response.status_code == 202, response.text
        run = response.json()["id"]
        assert app.state.store.claim() is None
        before = app.state.store.get("tenant", run)
        events = app.state.store.events("tenant", run)
        response = client.post(f"/workflows/{run}/approve/publish",
                               headers={"Authorization": "Bearer approver"}, json={"approved": value})
        assert response.status_code == 422, response.text
        assert app.state.store.get("tenant", run) == before
        assert app.state.store.events("tenant", run) == events
        assert app.state.store.claim() is None
        accepted = client.post(f"/workflows/{run}/approve/publish",
                               headers={"Authorization": "Bearer approver"}, json={"approved": True})
        assert accepted.status_code == 200, accepted.text
        assert app.state.store.claim() is not None


@pytest.mark.integration
async def test_rag_credential_transport_ignores_ambient_proxy_and_ca(provider, monkeypatch, tmp_path):
    import json

    model, store, run_id, state = provider
    config = replace(model.config, rag_url=model.config.routes[0].base_url,
                     rag_keys={"tenant-a": "dedicated-rag-fixture"})
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "unselected-ca.pem"))
    state["replies"]["primary"]["body"] = json.dumps(rag_reply()).encode()
    tools = Tools(config, model, store)
    result = await tools.execute({"tool": "rag.search", "args": {}, "tenant": "tenant-a",
                                  "run_id": run_id, "goal": "Find session policy"}, {})
    assert result["citations"][0]["document_id"] == "session"
    request, = state["calls"]
    assert request["path"] == "/primary/query"
    assert request["headers"]["authorization"] == "Bearer dedicated-rag-fixture"
    assert request["payload"]["generate"] is False
