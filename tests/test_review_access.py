"""Whole-review regressions for namespace accounting and provider capability drift."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest

from agentic.api import create_app
from agentic.auth import Principal, ROLE_SCOPES
from agentic.config import MODEL_PROVIDERS, Settings
from agentic.features import Features
from agentic import mcp_server
from agentic.schemas import Step, WorkflowIn
from test_features_mcp import bridge as bridge

pytestmark = pytest.mark.integration


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"fixture-key":"tenant"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"tenant":"fixture-rag"}')
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENT_RATE_DB_PATH", str(tmp_path / "limits.db"))
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_WEBHOOKS_JSON", "{}")
    for name in ("AGENT_DATABASE_URL", "AGENT_MODEL_ROUTES_JSON", "AGENT_REDIS_URL",
                 "AGENT_OIDC_ISSUER", "AGENT_OIDC_AUDIENCE", "AGENT_OIDC_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    return replace(Settings.from_env(), db_path=str(tmp_path / "agent.db"))


@pytest.mark.parametrize("namespaces,expected", [
    (None, {"default", "private", "legacy", "unbound"}),
    (["default"], {"default", "legacy"}),
    (["private"], {"private"}),
    (["private", "default"], {"default", "private", "legacy"}),
    ([], set()),
])
def test_usage_authorization_filters_calls_and_every_total(configuration, monkeypatch, namespaces, expected):
    app = create_app(configuration)
    store = app.state.store
    ids, costs, tokens = {}, {}, {}
    for index, label in enumerate(("default", "private", "legacy", "unbound", "foreign"), 1):
        tenant = "foreign" if label == "foreign" else "tenant"
        specification = {} if label == "legacy" else {"memory_namespace": label}
        run = None if label == "unbound" else store.submit(tenant, WorkflowIn(goal="Calculate 6 * 7", steps=[
            Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
        ]), specification=specification)["id"]
        ids[label] = run
        costs[label], tokens[label] = index * 3, index * 10
        reservation = store.reserve_usage(tenant, run, label, "synthesis", tokens[label], costs[label])
        store.settle_usage(reservation, input_tokens=tokens[label] - 1, output_tokens=1,
                           cost_microusd=costs[label])
    principal = Principal("tenant", "fixture-reader", frozenset({"reader"}),
                          frozenset(ROLE_SCOPES["reader"]), {"memory_namespaces": namespaces})
    monkeypatch.setattr(app.state.authenticator, "authenticate", lambda _: principal)
    with TestClient(app) as client:
        response = client.get("/usage", headers={"Authorization": "Bearer fixture-key"})
        assert response.status_code == 200
        usage = response.json()
        assert {record["route"] for record in usage["calls"]} == expected
        assert usage["charged_tokens"] == sum(tokens[label] for label in expected)
        assert usage["known_cost_usd"] == usage["estimated_cost_usd"] == sum(costs[label] for label in expected) / 1e6
        assert usage["cost_complete"] and usage["usage_complete"]
        denied = client.get("/usage", params={"run_id": ids["private"]})
        assert denied.status_code == (200 if namespaces is None or "private" in namespaces else 403)
        assert client.get("/usage", params={"run_id": ids["foreign"]}).status_code == 404
        assert client.get("/usage", params={"run_id": ""}).status_code == 404


@pytest.mark.parametrize("provider", sorted(MODEL_PROVIDERS))
async def test_configuration_api_and_mcp_share_every_resolved_provider(configuration, monkeypatch, bridge, provider):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TINKER_API_KEY"):
        monkeypatch.setenv(name, "disposable-provider-capability-fixture")
        monkeypatch.delenv(name + "_FILE", raising=False)
    if provider == "routed":
        monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
        monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
            "preset": "inkling-local", "auth_mode": "none",
        }]))
    else:
        monkeypatch.setenv("AGENT_MODEL_PROVIDER", provider)
    config = replace(Settings.from_env(), db_path=configuration.db_path)
    assert config.provider == provider
    with TestClient(create_app(config)) as client:
        response = client.get("/capabilities", headers={"Authorization": "Bearer fixture-key"})
        assert response.status_code == 200
        bridge["capabilities"] = response.json()
    assert bridge["capabilities"]["model"]["provider"] == provider
    server = mcp_server.create_server(Features())
    assert {tool.name for tool in await server.list_tools()} == {
        "start_research_workflow", "get_workflow_status", "get_workflow_events",
    }
