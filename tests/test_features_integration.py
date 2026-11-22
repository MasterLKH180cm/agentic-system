"""Actual API/storage/worker boundaries for disabled and snapshotted features."""
from contextlib import ExitStack
from dataclasses import replace
from html.parser import HTMLParser

from fastapi.testclient import TestClient
import pytest

from agentic.api import create_app
from agentic.config import Settings
from agentic.features import FEATURE_DEFAULTS, FeatureDisabled, Features
from agentic.memory import Memory
from agentic.model import ModelGateway
from agentic.schemas import Step
from agentic.storage_adapters import ArtifactStore
from agentic.telemetry import Telemetry
from agentic.tools import Tools
from agentic.worker import Worker

pytestmark = pytest.mark.integration
REQUESTER = {"Authorization": "Bearer requester-feature-key"}
APPROVER = {"Authorization": "Bearer approver-feature-key"}
CALCULATE = {"id": "calculate", "tool": "math.calculate", "args": {"expression": "6 * 7"}}
TOOL_CASES = [
    ("rag_search", {"id": "read", "tool": "rag.search"}),
    ("synthesis", {"id": "answer", "tool": "model.summarize"}),
    ("math", CALCULATE),
    ("merge", {"id": "merge", "tool": "data.merge"}),
    ("memory", {"id": "recall", "tool": "memory.search"}),
    ("artifacts", {"id": "publish", "tool": "artifact.publish"}),
    ("approvals", {**CALCULATE, "approval_required": True}),
]


@pytest.fixture
def factory(monkeypatch, tmp_path):
    for name in ("AGENT_DATABASE_URL", "AGENT_FEATURES_FILE", "AGENT_FEATURES_JSON", "AGENT_MODEL_ROUTES_JSON",
                 "AGENT_WEBHOOKS_JSON", "OPENAI_API_KEY_FILE", "ANTHROPIC_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"requester-feature-key":"tenant-a"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"tenant-a":"private-rag-secret"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"approver-feature-key":"tenant-a"}')
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENT_ARTIFACT_BACKEND", "local")
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "0")
    monkeypatch.setenv("AGENT_TENANT_POLICIES_JSON", "{}")
    config = Settings.from_env()
    count = 0
    with ExitStack() as stack:
        def make(flags=None, **settings):
            nonlocal count
            count += 1
            options = {"db_path": str(tmp_path / f"runs-{count}.db"), "features": Features(flags or {}), **settings}
            configured = replace(config, **options)
            app = create_app(configured)
            return stack.enter_context(TestClient(app)), app, configured
        yield make


def assert_disabled(response, feature):
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {
        "code": "feature_disabled", "feature": feature, "reason": "Feature disabled: " + feature,
    }


@pytest.mark.parametrize("feature,step", TOOL_CASES)
def test_each_public_tool_rejects_before_admission(factory, feature, step):
    client, app, _ = factory({feature: False})
    response = client.post("/workflows", headers=REQUESTER, json={"goal": "Bounded operation", "steps": [step]})
    assert_disabled(response, feature)
    assert app.state.store.list("tenant-a") == []
    if feature != "approvals":
        tools = client.get("/tools", headers=REQUESTER).json()
        assert step["tool"] not in tools["allowlisted"]
        assert next(item for item in tools["definitions"] if item["name"] == step["tool"])["enabled"] is False


@pytest.mark.parametrize("feature,payload", [
    ("dag", {"goal": "calculate 6 * 7", "steps": [CALCULATE]}),
    ("agent_loop", {"goal": "calculate 6 * 7", "runtime_mode": "agent_loop"}),
    ("registry", {"goal": "calculate 6 * 7", "policy_id": "recorded-policy"}),
    ("rag_search", {"goal": "Find the timeout policy"}),
    ("synthesis", {"goal": "Find the timeout policy"}),
    ("approvals", {"goal": "Publish reviewed report", "steps": [{"id": "publish", "tool": "artifact.publish"}]}),
])
def test_mode_references_and_implicit_plans_cannot_bypass_flags(factory, feature, payload):
    client, app, _ = factory({feature: False})
    assert_disabled(client.post("/workflows", headers=REQUESTER, json=payload), feature)
    assert app.state.store.list("tenant-a") == []


@pytest.mark.parametrize("feature,method,path,payload", [
    ("registry", "GET", "/registry/agents", None),
    ("registry", "GET", "/registry/agents/research/1", None),
    ("registry", "POST", "/registry/policies", {"name": "read", "version": 1}),
    ("memory", "PUT", "/memory/default/note", {"content": "Session timeout is 30 minutes"}),
    ("memory", "GET", "/memory/default/note", None),
    ("memory", "GET", "/memory/default?q=session", None),
    ("memory", "DELETE", "/memory/default/note", None),
    ("artifacts", "GET", "/artifacts/unknown", None),
    ("sse", "GET", "/workflows/unknown/events/stream", None),
])
def test_disabled_http_surfaces_still_require_authentication(factory, feature, method, path, payload):
    client, app, _ = factory({feature: False})
    kwargs = {} if payload is None else {"json": payload}
    assert client.request(method, path, **kwargs).status_code == 401
    assert_disabled(client.request(method, path, headers=REQUESTER, **kwargs), feature)
    assert app.state.store.list("tenant-a") == []


def test_mcp_header_gates_even_read_and_submission_without_weakening_http_auth(factory):
    client, app, _ = factory({"mcp": False})
    bridge = {**REQUESTER, "X-Agent-Interface": "mcp"}
    assert client.get("/capabilities").status_code == 401
    assert client.get("/capabilities", headers=REQUESTER).status_code == 200
    assert_disabled(client.get("/capabilities", headers=bridge), "mcp")
    assert_disabled(client.post("/workflows", headers=bridge,
                               json={"goal": "calculate 6 * 7", "steps": [CALCULATE]}), "mcp")
    assert app.state.store.list("tenant-a") == []


def test_disabled_optional_initialization_and_secret_free_capabilities(factory, monkeypatch):
    monkeypatch.setenv("AGENT_ARTIFACT_BACKEND", "s3")
    monkeypatch.delenv("AGENT_S3_BUCKET", raising=False)
    monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENT_WEBHOOKS_JSON", "invalid-unused-hook-secret")
    client, app, _ = factory(dict.fromkeys(FEATURE_DEFAULTS, False), provider="openai",
                             openai_key="private-provider-secret", rag_url="https://private-endpoint.invalid")
    assert app.state.tools.artifacts is None
    assert app.state.telemetry.enabled is False and app.state.telemetry.provider is None
    capabilities = client.get("/capabilities", headers=REQUESTER)
    assert capabilities.status_code == 200
    value = capabilities.json()
    assert value["features"] == dict.fromkeys(FEATURE_DEFAULTS, False)
    assert value["storage"] == {"backend": "sqlite", "memory_search": "lexical"}
    assert value["model"] == {"provider": "openai", "generation_enabled": False, "fallback_enabled": False}
    assert not any(tool["enabled"] for tool in value["tools"])
    assert all(secret not in capabilities.text for secret in (
        "private-provider-secret", "private-rag-secret", "private-endpoint", "requester-feature-key", "runs-",
    ))
    for path in ("/", "/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert_disabled(client.post("/hooks/workflows", json={}), "webhooks")
    with app.state.store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='hook_receipts'").fetchone() is None
    with pytest.raises(FeatureDisabled, match="artifacts"):
        ArtifactStore(Features({"artifacts": False}))
    monkeypatch.setenv("AGENT_FEATURES_JSON", '{"telemetry":false}')
    assert Telemetry().enabled is False


def test_dashboard_does_not_expose_disabled_controls(factory):
    client, _, _ = factory({"agent_loop": False, "approvals": False, "sse": False, "api_docs": False})
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []

        def handle_starttag(self, tag, attrs):
            self.items.append((tag, dict(attrs)))
    page = client.get("/")
    elements = Elements()
    elements.feed(page.text)
    controls = {attrs["id"]: attrs for _, attrs in elements.items if "id" in attrs}
    for name in ("stream", "stop", "approve", "reject", "step"):
        assert "disabled" in controls[name]
        assert controls[name]["data-feature-disabled"] == "true"
    assert "disabled" not in controls["submit"]
    modes = {attrs["value"]: attrs for tag, attrs in elements.items if tag == "option"}
    assert "disabled" in modes["agent_loop"] and "disabled" not in modes["dag"]
    assert not any(attrs.get("href") == "/docs" for _, attrs in elements.items)
    assert "sha256-" in page.headers["Content-Security-Policy"]
    online, _, _ = factory({"model_generation": False}, provider="openai")
    assert 'id="submit" disabled data-feature-disabled="true"' in online.get("/").text


@pytest.mark.parametrize("feature,step", TOOL_CASES)
async def test_worker_rechecks_current_permissions_after_a_valid_submission(factory, feature, step, tmp_path):
    client, app, config = factory()
    response = client.post("/workflows", headers=REQUESTER, json={"goal": "Bounded operation", "steps": [step]})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    if Step.model_validate(step).approval_required:
        assert app.state.store.claim() is None
        assert client.post(f"/workflows/{run_id}/approve/{step['id']}", headers=APPROVER,
                           json={"approved": True}).status_code == 200
    restarted = replace(config, features=Features({feature: False}))
    worker = Worker(app.state.store, Tools(restarted, ModelGateway(restarted, app.state.store), app.state.store))
    assert await worker.once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "failed"
    result = run["steps"][0]["result"]
    assert (result["outcome"], result["stop_reason"], result["feature"]) == (
        "policy_denied", "feature_disabled", feature,
    )
    assert app.state.store.usage("tenant-a", run_id)["calls"] == []
    assert not (tmp_path / "artifacts").exists()


async def test_old_snapshot_cannot_be_relaxed_by_enabling_worker_math(factory):
    client, app, config = factory({"math": False, "memory": False})
    response = client.post("/workflows", headers=REQUESTER,
                           json={"goal": "calculate 6 * 7", "runtime_mode": "agent_loop"})
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert app.state.store.get("tenant-a", run_id)["specification"]["feature_flags"]["math"] is False
    enabled = replace(config, features=Features())
    assert await Worker(app.state.store, Tools(enabled, ModelGateway(enabled, app.state.store))).once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "failed"
    assert run["steps"][0]["result"]["feature"] == "math"


@pytest.mark.parametrize("finished", [False, True])
async def test_recovered_agent_checkpoint_revalidates_completed_and_pending_plans(factory, finished):
    client, app, config = factory()
    response = client.post("/workflows", headers=REQUESTER,
                           json={"goal": "calculate 6 * 7", "runtime_mode": "agent_loop"})
    run_id = response.json()["id"]
    original = app.state.store.claim()
    state = {"version": 2, "plan": [CALCULATE], "finished": finished,
             "memory": {"citations": []}, "result": {"outcome": "success", "answer": "42"}}
    assert app.state.store.checkpoint(original, state)
    with app.state.store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run_id,))
    disabled = replace(config, features=Features({"math": False}))
    assert await Worker(app.state.store, Tools(disabled, ModelGateway(disabled, app.state.store))).once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "failed" and run["steps"][0]["attempts"] == 2
    assert run["steps"][0]["result"]["feature"] == "math"
    assert not app.state.store.complete(original, {"outcome": "success"})


async def test_new_memory_disabled_loop_runs_without_implicit_recall(factory, monkeypatch):
    client, app, _ = factory({"memory": False})
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled memory was accessed")
    monkeypatch.setattr(Memory, "search", forbidden)
    response = client.post("/workflows", headers=REQUESTER,
                           json={"goal": "calculate 6 * 7", "runtime_mode": "agent_loop"})
    run_id = response.json()["id"]
    assert await Worker(app.state.store, app.state.tools).once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "completed" and run["steps"][0]["result"]["answer"] == "42"


def test_approval_disable_preserves_pending_state_and_cancellation(factory):
    client, app, config = factory()
    result = client.post("/workflows", headers=REQUESTER, json={
        "goal": "Publish reviewed report", "steps": [{"id": "publish", "tool": "artifact.publish"}],
    })
    run_id = result.json()["id"]
    assert app.state.store.claim() is None
    disabled, _, _ = factory({"approvals": False}, db_path=config.db_path)
    assert_disabled(disabled.post(f"/workflows/{run_id}/approve/publish", headers=APPROVER,
                                 json={"approved": True}), "approvals")
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "waiting_approval" and not run["steps"][0]["approved"]
    assert disabled.post(f"/workflows/{run_id}/cancel", headers=REQUESTER).status_code == 200


def test_duplicate_submission_cannot_bypass_newly_disabled_mode(factory):
    client, app, config = factory()
    payload = {"goal": "calculate 6 * 7", "steps": [CALCULATE], "idempotency_key": "fixed-work"}
    assert client.post("/workflows", headers=REQUESTER, json=payload).status_code == 202
    disabled, _, _ = factory({"dag": False}, db_path=config.db_path)
    assert_disabled(disabled.post("/workflows", headers=REQUESTER, json=payload), "dag")
    assert len(app.state.store.list("tenant-a")) == 1


@pytest.mark.parametrize("feature", ["dag", "agent_loop", "registry", "memory", "model_generation"])
async def test_worker_rechecks_modes_references_and_implicit_dependencies(factory, feature):
    client, app, config = factory(provider="openai" if feature == "model_generation" else "offline")
    payload = {"goal": "calculate 6 * 7"}
    if feature in {"agent_loop", "memory"}:
        payload["runtime_mode"] = "agent_loop"
    elif feature != "model_generation":
        payload["steps"] = [CALCULATE]
    if feature == "registry":
        assert client.post("/registry/policies", headers=REQUESTER, json={
            "name": "arithmetic", "version": 1, "allowed_tools": ["math.calculate"],
        }).status_code == 201
        payload["policy_id"] = "arithmetic"
    response = client.post("/workflows", headers=REQUESTER, json=payload)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    disabled = replace(config, features=Features({feature: False}))
    assert await Worker(app.state.store, Tools(disabled, ModelGateway(disabled, app.state.store))).once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "failed"
    assert run["steps"][0]["result"]["feature"] == feature
    assert run["steps"][0]["result"]["stop_reason"] == "feature_disabled"
    assert app.state.store.usage("tenant-a", run_id)["calls"] == []


def test_registry_templates_and_references_revalidate_tool_permissions(factory):
    template = {"name": "arithmetic", "version": 1, "steps": [CALCULATE]}
    client, _, config = factory()
    assert client.post("/registry/workflows", headers=REQUESTER, json=template).status_code == 201
    disabled, app, _ = factory({"math": False, "agent_loop": False}, db_path=config.db_path)
    assert_disabled(disabled.post("/registry/workflows", headers=REQUESTER,
                                  json={**template, "version": 2}), "math")
    assert_disabled(disabled.post("/registry/agents", headers=REQUESTER,
                                  json={"name": "research", "version": 1}), "agent_loop")
    assert_disabled(disabled.post("/workflows", headers=REQUESTER,
                                  json={"goal": "calculate 6 * 7", "workflow_id": "arithmetic"}), "math")
    assert app.state.store.list("tenant-a") == []
    assert len(app.state.store.definitions("tenant-a", "workflows")) == 1
