import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from agentic.api import create_app
from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.tools import Tools, safe_calculate
from agentic.worker import Worker


A = {"Authorization": "Bearer a-key"}
B = {"Authorization": "Bearer b-key"}
APPROVER_A = {"Authorization": "Bearer approve-a"}


@pytest.fixture
def system(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"a-key":"tenant-a","b-key":"tenant-b"}')
    monkeypatch.setenv(
        "AGENT_APPROVER_KEYS_JSON",
        '{"approve-a":"tenant-a","approve-b":"tenant-b"}',
    )
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENT_RAG_API_KEY", "rag-key")
    config = Settings.from_env()
    app = create_app(config)
    with TestClient(app) as client:
        worker = Worker(app.state.store, Tools(config, ModelGateway(config)))
        yield client, worker, app.state.store


def submit(client, steps, goal="Test durable workflow", **kwargs):
    return client.post(
        "/workflows", headers=A,
        json={"goal": goal, "steps": steps, **kwargs},
    )


def test_parallel_dag_and_idempotency(system):
    client, worker, store = system
    steps = [
        {"id": "left", "tool": "math.calculate", "args": {"expression": "2 + 3"}},
        {"id": "right", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
        {"id": "merge", "tool": "data.merge", "depends_on": ["left", "right"]},
    ]
    result = submit(client, steps, idempotency_key="run-1")
    assert result.status_code == 202, result.text
    run_id = result.json()["id"]
    again = submit(client, steps, idempotency_key="run-1")
    assert again.json() == {"id": run_id, "duplicate": True}
    assert submit(client, steps, goal="Different", idempotency_key="run-1").status_code == 409
    assert asyncio.run(worker.once()) == 2
    interim = client.get("/workflows/" + run_id, headers=A).json()
    assert sum(s["status"] == "completed" for s in interim["steps"]) == 2
    assert asyncio.run(worker.once()) == 1
    completed = client.get("/workflows/" + run_id, headers=A).json()
    assert completed["status"] == "completed"
    assert completed["steps"][2]["result"]["outputs"]["left"]["value"] == 5
    assert completed["steps"][2]["result"]["outputs"]["right"]["value"] == 42
    assert client.get("/workflows/" + run_id, headers=B).status_code == 404
    assert client.get("/workflows/" + run_id + "/events", headers=A).status_code == 200
    assert len(store.events("tenant-a", run_id)) >= 5


def test_human_approval_separate_role(system):
    client, worker, _ = system
    response = submit(client, [
        {"id": "calc", "tool": "math.calculate", "args": {"expression": "9 * 9"}},
        {"id": "publish", "tool": "artifact.publish", "depends_on": ["calc"],
         "approval_required": False},
    ])
    assert response.status_code == 202
    run_id = response.json()["id"]
    asyncio.run(worker.once())
    assert asyncio.run(worker.once()) == 0
    state = client.get("/workflows/" + run_id, headers=A).json()
    assert state["status"] == "waiting_approval"
    assert state["steps"][1]["approval_required"]
    path = f"/workflows/{run_id}/approve/publish"
    assert client.post(path, headers=A, json={"approved": True}).status_code == 403
    assert client.post(path, headers=APPROVER_A, json={"approved": True}).status_code == 200
    assert asyncio.run(worker.once()) == 1
    state = client.get("/workflows/" + run_id, headers=A).json()
    assert state["status"] == "completed"
    assert state["steps"][1]["result"]["published"]


def test_dag_validation_and_tenant_isolation(system):
    client, worker, _ = system
    assert client.get("/workflows").status_code == 401
    assert submit(client, [
        {"id": "a", "tool": "data.merge", "depends_on": ["b"]},
        {"id": "b", "tool": "data.merge", "depends_on": ["a"]},
    ]).status_code == 422
    assert submit(client, [{"id": "shell", "tool": "shell.execute"}]).status_code == 422
    r = submit(client, [{"id": "t", "tool": "math.calculate", "args": {"expression": "1"}}])
    run_id = r.json()["id"]
    assert client.post("/workflows/" + run_id + "/cancel", headers=B).status_code == 409
    assert client.post("/workflows/" + run_id + "/cancel", headers=A).status_code == 200
    assert asyncio.run(worker.once()) == 0


def test_fencing_and_recovery(system):
    client, _, store = system
    response = submit(client, [{"id": "math", "tool": "math.calculate",
                                "args": {"expression": "6 + 5"}}])
    run_id = response.json()["id"]
    claimed = store.claim()
    assert claimed is not None
    stolen = {**claimed, "lease_token": "old-or-forged"}
    assert not store.complete(stolen, {"value": -1})
    with store.connect() as conn:
        conn.execute(
            "UPDATE steps SET lease_until=0 WHERE run_id=? AND id='math'", (run_id,)
        )
    reclaimed = store.claim()
    assert reclaimed["lease_token"] != claimed["lease_token"]
    assert not store.complete(claimed, {"value": 1})
    assert store.complete(reclaimed, {"value": 11})
    assert store.get("tenant-a", run_id)["status"] == "completed"


def test_real_rag_tool_contract_via_mock_http(system, monkeypatch):
    client, worker, _ = system
    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        def handler(request):
            assert request.headers["authorization"] == "Bearer rag-key"
            assert request.url.path == "/query"
            return httpx.Response(200, json={
                "answer": "Sessions expire after 30 minutes.",
                "citations": [{"chunk_id": "c1", "document_id": "doc1"}],
                "mode_used": "hybrid",
            })
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    result = client.post("/workflows", headers=A, json={
        "goal": "What is the session timeout?"
    })
    assert result.status_code == 202, result.text
    run_id = result.json()["id"]
    assert asyncio.run(worker.once()) == 1
    assert asyncio.run(worker.once()) == 1
    state = client.get("/workflows/" + run_id, headers=A).json()
    assert state["status"] == "completed"
    assert state["steps"][1]["result"]["citations"][0]["chunk_id"] == "c1"


def test_calculator_and_config_fail_closed(monkeypatch):
    assert safe_calculate("(2+3)*4") == 20
    for bad in ("__import__('os')", "10**100", "2/0", "1e999", "2**3**4**5"):
        with pytest.raises((ValueError, ZeroDivisionError, SyntaxError, OverflowError)):
            safe_calculate(bad)
    monkeypatch.setenv("AGENT_ENV", "production")
    monkeypatch.delenv("AGENT_KEYS_JSON", raising=False)
    with pytest.raises(ValueError, match="AGENT_KEYS_JSON"):
        Settings.from_env()
