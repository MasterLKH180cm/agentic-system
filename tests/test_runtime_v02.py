import asyncio
from dataclasses import replace
import json
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from agentic.api import create_app
from agentic.config import Settings
from agentic.runtime import AgentRuntime, evaluate
from agentic.store import Store
from agentic.worker import Worker


A = {"Authorization": "Bearer a-key"}
B = {"Authorization": "Bearer b-key"}


@pytest.fixture
def system(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"a-key":"a","b-key":"b"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"rag-a","b":"rag-b"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"approve-a":"a"}')
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_TENANT_POLICIES_JSON", json.dumps({
        "a": {"allowed_namespaces": ["default", "notes"]},
        "b": {"allowed_namespaces": ["default", "notes"]},
    }))
    app = create_app(Settings.from_env())
    with TestClient(app) as client:
        yield client, app.state.store, app.state.tools, app


def submit(client, **payload):
    response = client.post("/workflows", headers=A, json={"goal": "What is the session timeout?", **payload})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def install_rag_fixture(monkeypatch, responses):
    original = httpx.AsyncClient
    calls = []

    def factory(*args, **kwargs):
        def handle(request):
            assert request.headers["authorization"] == "Bearer rag-a"
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=responses[min(len(calls) - 1, len(responses) - 1)])
        return original(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


EVIDENCE = {
    "answer": "Session timeout is 30 minutes. [c1]", "mode_used": "hybrid",
    "citations": [{"chunk_id": "c1", "document_id": "session", "source": "policy",
                   "text": "Session timeout is 30 minutes."}],
}


def test_registry_versions_policy_intersection_and_snapshots(system):
    client, store, _, _ = system
    definition = {"name": "research", "version": 1, "instructions": "Read evidence", "max_rounds": 2}
    assert client.post("/registry/agents", headers=A, json=definition).status_code == 201
    assert client.post("/registry/agents", headers=A, json=definition).status_code == 409
    assert client.get("/registry/agents/research/1", headers=B).status_code == 404
    run_id = submit(client, runtime_mode="agent_loop", agent_id="research", agent_version=1)
    assert client.post("/registry/agents", headers=A, json={**definition, "version": 2,
                                                         "instructions": "Changed"}).status_code == 201
    state = store.get("a", run_id)
    assert state["specification"]["definitions"]["agent"]["instructions"] == "Read evidence"
    assert state["specification"]["max_rounds"] == 2
    denied = client.post("/workflows", headers=A, json={"goal": "Calculate 2+2", "max_model_tokens": 13000})
    assert denied.status_code == 403
    policy = {"name": "math-only", "version": 1, "allowed_tools": ["math.calculate"]}
    assert client.post("/registry/policies", headers=A, json=policy).status_code == 201
    assert client.post("/workflows", headers=A, json={"goal": "Find policy", "policy_id": "math-only"}).status_code == 403
    assert client.post("/workflows", headers=A, json={"goal": "Find policy", "steps": [
        {"id": "memory", "tool": "memory.search", "args": {"namespace": "secret"}}
    ]}).status_code == 403
    assert client.post("/workflows", headers=A, json={"goal": "Find policy", "steps": [
        {"id": "read", "tool": "rag.search", "args": {"url": "https://attacker.invalid"}}
    ]}).status_code == 422


def test_memory_tenant_namespace_ttl_and_provenance(system):
    client, store, _, _ = system
    assert client.put("/memory/notes/session", headers=A,
                      json={"content": "Session timeout is 30 minutes", "ttl_seconds": 60}).status_code == 200
    assert client.get("/memory/notes/session", headers=B).status_code == 404
    body = client.get("/memory/notes?q=session", headers=A).json()
    assert body["search_type"] == "lexical" and body["records"][0]["id"] == "session"
    assert client.put("/memory/secret/session", headers=A, json={"content": "Denied"}).status_code == 403
    other_run = client.post("/workflows", headers=B, json={"goal": "calculate 2+2"}).json()["id"]
    assert client.put("/memory/notes/foreign", headers=A,
                      json={"content": "Foreign provenance", "source_run_id": other_run}).status_code == 422
    with store.connect() as db:
        db.execute("UPDATE memories SET expires_at=0 WHERE tenant='a'")
    assert client.get("/memory/notes/session", headers=A).status_code == 404
    assert client.get("/memory/notes?q=session", headers=A).json()["records"] == []
    assert client.delete("/memory/notes/session", headers=A).status_code == 200


def test_loop_refines_and_records_evidence(system, monkeypatch):
    client, store, tools, _ = system
    calls = install_rag_fixture(monkeypatch, [
        {"answer": "Insufficient evidence.", "citations": [], "mode_used": "hybrid"}, EVIDENCE,
    ])
    run_id = submit(client, runtime_mode="agent_loop")
    assert asyncio.run(Worker(store, tools).once()) == 1
    run = store.get("a", run_id)
    assert run["status"] == "completed"
    result = run["steps"][0]["result"]
    assert result["outcome"] == "success" and result["rounds"] == 2
    assert [entry["accepted"] for entry in result["evaluations"]] == [False, True]
    assert result["citations"][0]["chunk_id"] == "c1"
    assert [request["mode"] for request in calls] == ["hybrid", "hybrid"]
    assert calls[0]["question"] != calls[1]["question"]
    assert all(request["generate"] is False for request in calls)
    assert any(e["type"] == "runtime_replan" for e in store.events("a", run_id))


def test_loop_exhaustion_is_failed_with_explicit_result(system, monkeypatch):
    client, store, tools, _ = system
    calls = install_rag_fixture(monkeypatch, [{"answer": "Insufficient evidence", "citations": []}])
    run_id = submit(client, runtime_mode="agent_loop", max_rounds=2)
    asyncio.run(Worker(store, tools).once())
    run = store.get("a", run_id)
    assert run["status"] == "failed" and len(calls) == 2
    assert run["steps"][0]["result"]["stop_reason"] == "rounds_exhausted"
    assert run["steps"][0]["result"]["citations"] == []


def test_loop_resumes_committed_observation_after_crash(system, monkeypatch):
    client, store, tools, _ = system
    calls = install_rag_fixture(monkeypatch, [EVIDENCE])
    run_id = submit(client, runtime_mode="agent_loop")
    job = store.claim()
    original = tools.execute

    class ProcessCrash(BaseException):
        pass

    async def crash_before_summary(nested, inputs):
        if nested["tool"] == "model.summarize":
            raise ProcessCrash()
        return await original(nested, inputs)

    monkeypatch.setattr(tools, "execute", crash_before_summary)
    with pytest.raises(ProcessCrash):
        asyncio.run(AgentRuntime(store, tools).run(job))
    assert store.get("a", run_id)["steps"][0]["checkpoint"]["cursor"] == 1
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run_id,))
    resumed = store.claim()
    monkeypatch.setattr(tools, "execute", original)
    result = asyncio.run(AgentRuntime(store, tools).run(resumed))
    assert result["outcome"] == "success" and len(calls) == 1
    assert not store.checkpoint(job, {"forged": True})
    assert not store.complete(job, {"forged": True})
    assert store.complete(resumed, result)


def test_cancelled_run_rejects_all_worker_mutations(system):
    client, store, _, _ = system
    run_id = submit(client, goal="calculate 2+2")
    job = store.claim()
    assert store.cancel("a", run_id)
    assert not store.heartbeat(job)
    assert not store.checkpoint(job, {})
    assert not store.complete(job, {"value": 4})
    assert not store.fail(job, "late failure")
    assert store.get("a", run_id)["status"] == "cancelled"
    assert store.events("a", run_id)[-1]["type"] == "cancelled"


def test_expired_leases_obey_attempt_limit(system):
    client, store, _, _ = system
    run_id = submit(client, goal="calculate 2+2")
    for attempt in range(store.max_attempts):
        assert store.claim()["attempts"] == attempt
        with store.connect() as db:
            db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run_id,))
    assert store.claim() is None
    run = store.get("a", run_id)
    assert run["status"] == "failed" and run["steps"][0]["attempts"] == 3
    assert any(e["type"] == "lease_exhausted" for e in store.events("a", run_id))


async def test_worker_renews_lease_and_cancels_in_flight(system, monkeypatch):
    client, store, tools, _ = system
    store.lease_seconds = 0.12
    tools.config = replace(tools.config, heartbeat_seconds=0.02, step_timeout_seconds=2)
    interrupted = asyncio.Event()

    async def slow(job, inputs):
        try:
            await asyncio.sleep(0.35)
            return {"value": 4}
        finally:
            interrupted.set()

    monkeypatch.setattr(tools, "execute", slow)
    first = submit(client, goal="calculate 2+2")
    worker = Worker(store, tools)
    task = asyncio.create_task(worker.process(store.claim()))
    await asyncio.sleep(0.2)
    assert store.claim() is None
    await task
    assert store.get("a", first)["status"] == "completed"
    interrupted.clear()
    second = submit(client, goal="calculate 3+3")
    task = asyncio.create_task(worker.process(store.claim()))
    await asyncio.sleep(0.04)
    assert store.cancel("a", second)
    await asyncio.wait_for(task, 0.3)
    assert interrupted.is_set() and store.get("a", second)["status"] == "cancelled"


async def test_worker_deadline_is_terminal(system, monkeypatch):
    client, store, tools, _ = system
    tools.config = replace(tools.config, step_timeout_seconds=0.03)

    async def slow(job, inputs):
        await asyncio.sleep(1)
        return {"value": 4}

    monkeypatch.setattr(tools, "execute", slow)
    run_id = submit(client, goal="calculate 2+2")
    await Worker(store, tools).once()
    state = store.get("a", run_id)
    assert state["status"] == "failed"
    assert state["steps"][0]["result"]["outcome"] == "deadline"


def test_evaluator_rejects_foreign_sources_numbers_and_wrong_arithmetic():
    observations = {
        "read": {"tool": "rag.search", "result": EVIDENCE},
        "answer": {"tool": "model.summarize", "result": {"answer": "Session timeout is 30 minutes [foreign]"}},
    }
    assert evaluate("What is the session timeout?", observations, "evidence")["reason"] == "unknown_citation"
    observations["answer"]["result"]["answer"] = "Session timeout is 60 minutes [c1]"
    assert evaluate("What is the session timeout?", observations, "evidence")["reason"] == "unsupported_numeric_claim"
    wrong_math = {"calc": {"tool": "math.calculate", "result": {"value": 5}}}
    assert not evaluate("calculate 2+2", wrong_math, "arithmetic")["accepted"]


def test_v01_migration_preserves_old_records_and_order(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE runs(id TEXT PRIMARY KEY, tenant TEXT, goal TEXT, status TEXT,
          idempotency_key TEXT, request_hash TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE steps(run_id TEXT,id TEXT,tool TEXT,args TEXT,depends_on TEXT,status TEXT,
          attempts INTEGER DEFAULT 0,available_at REAL DEFAULT 0,lease_until REAL,lease_token TEXT,
          approved INTEGER DEFAULT 0,approval_required INTEGER DEFAULT 0,result TEXT,error TEXT,
          PRIMARY KEY(run_id,id));
        INSERT INTO runs VALUES('old','a','Old goal','queued',NULL,'hash','2020','2020');
        INSERT INTO steps(run_id,id,tool,args,depends_on,status)
          VALUES('old','z','data.merge','{}','[]','pending');
        INSERT INTO steps(run_id,id,tool,args,depends_on,status)
          VALUES('old','a','data.merge','{}','["z"]','pending');
        """)
    store = Store(path)
    state = store.get("a", "old")
    assert state["specification"] == {}
    assert [step["id"] for step in state["steps"]] == ["z", "a"]
    assert state["steps"][0]["checkpoint"] == {}
    assert store.claim()["id"] == "z"


def test_persisted_deadline_survives_reclaim_and_stops_run(system):
    client, store, _, _ = system
    run_id = submit(client, goal="calculate 2+2")
    original = store.claim()
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run_id,))
    recovered = Store(store.path).claim()
    assert recovered["deadline_at"] == original["deadline_at"]
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0,deadline_at=0 WHERE run_id=?", (run_id,))
    assert store.claim() is None
    state = store.get("a", run_id)
    assert state["status"] == "failed"
    assert state["steps"][0]["result"]["stop_reason"] == "persisted_step_deadline"


def test_resume_after_evaluation_does_not_duplicate_round(system, monkeypatch):
    client, store, tools, _ = system
    calls = install_rag_fixture(monkeypatch, [EVIDENCE])
    run_id = submit(client, runtime_mode="agent_loop")
    original_checkpoint = store.checkpoint

    class ProcessCrash(BaseException):
        pass

    def crash_after_evaluation(job, state, **kwargs):
        accepted = original_checkpoint(job, state, **kwargs)
        if kwargs.get("event") == "round_evaluated":
            raise ProcessCrash()
        return accepted

    monkeypatch.setattr(store, "checkpoint", crash_after_evaluation)
    with pytest.raises(ProcessCrash):
        asyncio.run(AgentRuntime(store, tools).run(store.claim()))
    monkeypatch.setattr(store, "checkpoint", original_checkpoint)
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run_id,))
    resumed = store.claim()
    result = asyncio.run(AgentRuntime(store, tools).run(resumed))
    assert len(result["evaluations"]) == 1 and len(calls) == 1
    assert sum(e["type"] == "round_evaluated" for e in store.events("a", run_id)) == 1
    assert store.complete(resumed, result)


def test_bound_namespace_protects_results_events_duplicates_and_artifacts(system, monkeypatch):
    client, store, tools, app = system
    assert client.put("/memory/notes/session", headers=A,
                      json={"content": "Session timeout is 30 minutes."}).status_code == 200
    payload = {"goal": "Collect the session note", "memory_namespace": "notes", "idempotency_key": "private-note",
               "steps": [{"id": "read", "tool": "memory.search", "args": {"namespace": "notes", "query": "session"}},
                         {"id": "publish", "tool": "artifact.publish", "depends_on": ["read"]}]}
    response = client.post("/workflows", headers=A, json=payload)
    assert response.status_code == 202
    run_id = response.json()["id"]
    worker = Worker(store, tools)
    asyncio.run(worker.once())
    asyncio.run(worker.once())
    assert client.post(f"/workflows/{run_id}/approve/publish", headers={"Authorization": "Bearer approve-a"},
                       json={"approved": True}).status_code == 200
    asyncio.run(worker.once())
    result = store.get("a", run_id)["steps"][-1]["result"]
    artifact_id = result["artifact_ref"]["artifact_id"]
    assert client.get("/artifacts/" + artifact_id, headers=A).status_code == 200
    auth = app.state.authenticator
    original = auth.authenticate
    restricted = auth._principal("a", "restricted", {"requester"}, attributes={"memory_namespaces": ["default"]})
    monkeypatch.setattr(auth, "authenticate", lambda header: restricted if header == "Bearer restricted" else original(header))
    headers = {"Authorization": "Bearer restricted"}
    for path in (f"/workflows/{run_id}", f"/workflows/{run_id}/events", "/usage?run_id=" + run_id,
                 "/artifacts/" + artifact_id, "/memory/notes/session"):
        assert client.get(path, headers=headers).status_code == 403, path
    assert client.get("/workflows", headers=headers).json() == []
    assert client.post("/workflows", headers=headers, json=payload).status_code == 403
    other_namespace = {**payload, "memory_namespace": "default", "idempotency_key": "escape"}
    assert client.post("/workflows", headers=A, json=other_namespace).status_code == 403


@pytest.mark.parametrize("answer", ["Session timeout is 30 minutes.", "Session timeout is 30 minutes [invented]."])
async def test_dag_summary_rejects_missing_or_fabricated_citations(system, monkeypatch, answer):
    client, store, tools, _ = system
    run_id = submit(client, goal="calculate 2+2")

    async def generation(system_prompt, message, **kwargs):
        parsed = json.loads(message)
        assert parsed["sources"][0]["chunk_id"] == "c1"
        return json.dumps({"outcome": "success", "answer": answer,
                           "cited_ids": ["invented"] if "[invented]" in answer else [], "refusal_reason": None})

    monkeypatch.setattr(tools.model, "complete", generation)
    job = {"tool": "model.summarize", "args": {}, "tenant": "a", "run_id": run_id,
           "goal": "What is the session timeout?", "specification": {}}
    with pytest.raises(ValueError, match="citation"):
        await tools.execute(job, {"retrieved": EVIDENCE})
    assert store.get("a", run_id)["steps"][0]["status"] == "pending"


async def test_summary_packs_complete_bounded_json_and_only_returns_cited_sources(system, monkeypatch):
    client, _, tools, _ = system
    run_id = submit(client, goal="calculate 2+2")
    captured = {}

    async def generation(system_prompt, message, **kwargs):
        captured["prompt"] = message
        captured["parsed"] = json.loads(message)
        return json.dumps({"outcome": "success", "answer": "Session timeout is 30 minutes [c1].",
                           "cited_ids": ["c1"], "refusal_reason": None})

    monkeypatch.setattr(tools.model, "complete", generation)
    sources = [{"chunk_id": f"c{i}", "document_id": f"doc{i}",
                "text": "Session timeout is 30 minutes. " + "context " * 400}
               for i in range(1, 21)]
    job = {"tool": "model.summarize", "args": {}, "tenant": "a", "run_id": run_id,
           "goal": "What is the session timeout?", "specification": {}}
    result = await tools.execute(job, {"retrieved": {"citations": sources}})
    assert len(captured["prompt"]) <= 14000
    assert captured["parsed"]["sources"] and captured["parsed"]["sources"][0]["text"]
    assert [source["chunk_id"] for source in result["citations"]] == ["c1"]
    assert result["citations"][0]["text_truncated"]


async def test_rag_response_stream_is_stopped_at_size_limit(system, monkeypatch):
    _, _, tools, _ = system
    original = httpx.AsyncClient
    chunks = []

    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(3):
                chunks.append(index)
                yield b"x" * 600000

    def factory(*args, **kwargs):
        def handle(request):
            assert json.loads(request.content)["generate"] is False
            return httpx.Response(200, stream=Oversized())
        return original(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    job = {"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture",
           "goal": "Find the session policy", "specification": {}}
    with pytest.raises(ValueError, match="size limit"):
        await tools.execute(job, {})
    assert chunks == [0, 1]


def test_separate_role_tokens_for_same_identity_cannot_self_approve(system, monkeypatch):
    client, store, tools, app = system
    auth = app.state.authenticator
    original = auth.authenticate
    principals = {
        "Bearer requester-jwt": auth._principal("a", "same-person", {"requester"}),
        "Bearer approver-jwt": auth._principal("a", "same-person", {"approver"}),
        "Bearer other-approver-jwt": auth._principal("a", "another-person", {"approver"}),
    }
    monkeypatch.setattr(auth, "authenticate", lambda header: principals.get(header) or original(header))
    response = client.post("/workflows", headers={"Authorization": "Bearer requester-jwt"}, json={
        "goal": "Calculate then publish", "steps": [
            {"id": "math", "tool": "math.calculate", "args": {"expression": "2+2"}},
            {"id": "publish", "tool": "artifact.publish", "depends_on": ["math"]},
        ],
    })
    assert response.status_code == 202
    run_id = response.json()["id"]
    worker = Worker(store, tools)
    asyncio.run(worker.once())
    asyncio.run(worker.once())
    path = f"/workflows/{run_id}/approve/publish"
    assert client.post(path, headers={"Authorization": "Bearer approver-jwt"}, json={"approved": True}).status_code == 403
    assert client.post(path, headers={"Authorization": "Bearer other-approver-jwt"}, json={"approved": True}).status_code == 200
