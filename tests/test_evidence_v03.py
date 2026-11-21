"""Admission/refusal invariants; these deterministic replies are contract fixtures."""
import asyncio
from contextlib import suppress
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from agentic.api import create_app
from agentic.config import Settings
from agentic.evidence import EvidenceBundle, collect_evidence, encode_json, extractive_answer, pack_evidence, refusal_result
from agentic.runtime import AgentRuntime, evaluate
from agentic.store import Store
from agentic.worker import Worker


A = {"Authorization": "Bearer a-key"}
PROFILE = "contract-fixture-profile"
SOURCE = {"chunk_id": "c1", "document_id": "session", "text": "Session timeout is 30 minutes."}
REAL_ASYNC_CLIENT = httpx.AsyncClient


def reply(*, sources=None, reason=None, calibrated=False, structured=None):
    sources = [dict(SOURCE)] if sources is None else sources
    count = len(sources)
    basis = "calibrated_reranker" if calibrated else "ranked_unassessed"
    if structured is not None:
        basis = "structured_exact"
    if reason:
        basis = {"no_candidates": "no_candidates", "model_rejected": "model_review",
                 "below_admission_threshold": "calibrated_reranker", "context_budget_exhausted": "context_budget"}[reason]
        count = 0 if reason == "no_candidates" else max(1, count)
    data = {
        "outcome": "insufficient_evidence" if reason else "success",
        "supported": False if reason else (True if structured is not None else None),
        "refusal_reason": reason,
        "admission": {
            "policy_version": "evidence-admission-v1", "decision": "abstain" if reason else (
                "accept" if calibrated or structured is not None else "unassessed"),
            "basis": basis, "profile_id": PROFILE if basis == "calibrated_reranker" else None,
            "score": (-1.0 if reason else 1.0) if basis == "calibrated_reranker" else None,
            "threshold": 0.0 if basis == "calibrated_reranker" else None,
            "candidate_count": count, "accepted_count": 0 if reason else len(sources),
        },
        "citations": [] if reason else sources, "answer": None, "mode_used": "hybrid",
        "retrieval_diagnostics": {"candidate_count": count, "candidate_ids": ["c1"] if count else [],
                                  "candidate_document_ids": ["session"] if count else [], "usable_as_evidence": False},
    }
    if structured is not None:
        data["structured_result"] = structured
    return data


def zero_result():
    return {"aggregate": {"operation": "count", "value": 0, "matched_rows": 0}, "matched_rows": 0,
            "provenance": {"datasets": [{"document_id": "orders", "revision": "r1", "profile_id": "index1"}]}}


def test_complete_exact_observation_survives_bounded_passage_packing():
    observation = {"step": "count", "kind": "structured", **zero_result()["aggregate"],
                   "expression": "count", "provenance": zero_result()["provenance"]}
    source = {**SOURCE, "text": SOURCE["text"] * 100, "text_truncated": False}
    bundle = EvidenceBundle(sources={"c1": source}, observations=[observation])
    exact_size = len(encode_json({"question": "Count orders", "sources": [], "observations": [observation]}).encode())
    prompt, _, observations = pack_evidence("Count orders", bundle, exact_size + 500)
    assert json.loads(prompt)["observations"] == observations == [observation]
    assert len(prompt.encode()) <= exact_size + 500
    assert pack_evidence("Count orders", bundle, exact_size - 1) == ("", [], [])
    # The offline answer has its own cap and must retain zero as well.
    assert extractive_answer([source], [observation], limit=100).startswith("count: count = 0\n")


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"a-key":"a"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"rag-a"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"approve-a":"a"}')
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("AGENT_RAG_ADMISSION_POLICY", "compatibility")
    monkeypatch.setenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", "[]")
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    config = Settings.from_env()
    app = create_app(config)
    with TestClient(app) as client:
        yield client, app.state.store, app.state.tools, config


def submit(client, **payload):
    response = client.post("/workflows", headers=A, json={"goal": "What is the session timeout?", **payload})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def rag_fixture(monkeypatch, replies):
    original, calls = REAL_ASYNC_CLIENT, []

    def factory(*args, **kwargs):
        def handle(request):
            assert request.headers["authorization"] == "Bearer rag-a"
            payload = json.loads(request.content)
            assert payload["generate"] is False
            calls.append(payload)
            return httpx.Response(200, json=replies[min(len(calls) - 1, len(replies) - 1)])
        return original(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


def test_no_evidence_dag_is_terminal_without_model_or_publication(system, monkeypatch):
    client, store, tools, _ = system
    calls = rag_fixture(monkeypatch, [reply(sources=[], reason="no_candidates")])

    async def forbidden(*args, **kwargs):
        raise AssertionError("No model should be called without usable evidence")

    monkeypatch.setattr(tools.model, "complete", forbidden)
    run = submit(client, steps=[
        {"id": "read", "tool": "rag.search"},
        {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]},
        {"id": "publish", "tool": "artifact.publish", "depends_on": ["answer"]},
    ])
    worker = Worker(store, tools)
    assert asyncio.run(worker.once()) == 1
    interim = store.get("a", run)
    assert interim["steps"][0]["status"] == "completed"
    assert interim["steps"][0]["result"]["retrieval"]["outcome"] == "insufficient_evidence"
    assert asyncio.run(worker.once()) == 1
    assert asyncio.run(worker.once()) == 0
    final = store.get("a", run)
    assert final["status"] == "failed"
    answer = final["steps"][1]
    assert answer["attempts"] == 1 and answer["result"]["outcome"] == "insufficient_evidence"
    assert answer["result"]["refusal_reason"] == "no_candidates" and answer["result"]["citations"] == []
    assert final["steps"][2]["status"] == "pending" and len(calls) == 1
    assert not store.usage("a", run)["calls"]


def test_operator_policy_is_validated_and_snapshotted(system, monkeypatch):
    _, _, _, config = system
    for raw in ('[]', '["duplicate","duplicate"]', '"not-array"', '[null]'):
        monkeypatch.setenv("AGENT_RAG_ADMISSION_POLICY", "calibrated_required")
        monkeypatch.setenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", raw)
        with pytest.raises(ValueError):
            Settings.from_env()
    monkeypatch.setenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", json.dumps([PROFILE]))
    required = Settings.from_env()
    app = create_app(required)
    with TestClient(app) as client:
        run = submit(client)
    snap = app.state.store.get("a", run)["specification"]
    assert snap["rag_admission_policy"] == "calibrated_required"
    assert snap["rag_admission_profile_ids"] == [PROFILE]
    app.state.tools.config = replace(config, rag_admission_policy="compatibility")
    assert app.state.tools.evidence_policy({"specification": snap}) == ("calibrated_required", [PROFILE])
    with pytest.raises(ValueError):
        replace(config, rag_admission_policy="unknown")


def test_rag_total_timeout_configuration_is_bounded(system, monkeypatch):
    _, _, _, config = system
    monkeypatch.delenv("AGENT_RAG_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().rag_timeout_seconds == 180
    for value in ("nan", "inf", "0", "361", "-1", "not-a-number"):
        monkeypatch.setenv("AGENT_RAG_TIMEOUT_SECONDS", value)
        with pytest.raises(ValueError):
            Settings.from_env()
    monkeypatch.setenv("AGENT_RAG_TIMEOUT_SECONDS", "2.5")
    assert Settings.from_env().rag_timeout_seconds == 2.5
    with pytest.raises(ValueError):
        replace(config, rag_timeout_seconds=True)


async def test_rag_timeout_covers_headers_and_entire_stream(system):
    _, _, tools, config = system
    handlers = []
    body = json.dumps(reply()).encode()

    async def handle(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            # Each individual network read is shorter than the configured second,
            # but headers plus the complete response exceed the total deadline.
            await asyncio.sleep(0.45)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(0.35)
            writer.write(body[:1])
            await writer.drain()
            await asyncio.sleep(0.35)
            writer.write(body[1:])
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    tools.config = replace(config, rag_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                           rag_timeout_seconds=1)
    try:
        with pytest.raises(TimeoutError):
            await tools.execute({"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture",
                                 "goal": "Find policy"}, {})
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("basis", ["ranked_unassessed", "model_review", "structured_exact", "no_candidates", "context_budget"])
@pytest.mark.parametrize("field,value", [("profile_id", PROFILE), ("score", 1.0), ("threshold", 0.0)])
async def test_uncalibrated_bases_reject_scoring_metadata(system, monkeypatch, basis, field, value):
    _, _, tools, _ = system
    if basis == "structured_exact":
        data = reply(sources=[], structured=zero_result())
    else:
        reason = {"model_review": "model_rejected", "no_candidates": "no_candidates",
                  "context_budget": "context_budget_exhausted"}.get(basis)
        data = reply(reason=reason)
    data["admission"][field] = value
    rag_fixture(monkeypatch, [data])
    with pytest.raises(ValueError, match="Uncalibrated"):
        await tools.execute({"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture",
                             "goal": "Find policy"}, {})


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(supported=False),
    lambda data: data["admission"].update(accepted_count=0),
    lambda data: data["admission"].update(decision="unknown"),
    lambda data: data["admission"].update(policy_version="future"),
    lambda data: data["admission"].update(score=float("inf")),
])
async def test_typed_contradictions_cannot_become_evidence(system, monkeypatch, mutation):
    _, _, tools, _ = system
    data = reply(calibrated=True)
    mutation(data)
    rag_fixture(monkeypatch, [data])
    job = {"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture", "goal": "Find policy"}
    with pytest.raises(ValueError):
        await tools.execute(job, {})


async def test_refusal_sources_are_quarantined_and_independent_branch_survives(system, monkeypatch):
    _, _, tools, _ = system
    refused = reply(reason="model_rejected")
    refused["citations"] = [SOURCE]
    rag_fixture(monkeypatch, [refused])
    job = {"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture", "goal": "Find policy"}
    with pytest.raises(ValueError, match="Refused"):
        await tools.execute(job, {})
    legacy = {"supported": False, "citations": [SOURCE]}
    rag_fixture(monkeypatch, [legacy])
    result = await tools.execute(job, {})
    assert result["answer"] is None and result["citations"] == [] and result["quarantined_ids"] == ["c1"]
    good = {**SOURCE, "chunk_id": "c2", "document_id": "independent"}
    merged = {"outputs": {"refused": result, "copied_memory": {"citations": [SOURCE]}, "valid": {"citations": [good]}}}
    bundle = collect_evidence({"merge": merged})
    assert list(bundle.sources) == ["c2"]
    assert bundle.refusal_reason == "model_rejected"


@pytest.mark.parametrize("calibrated,profile,allowed", [
    (False, PROFILE, False), (True, "not-approved", False), (True, PROFILE, True),
])
async def test_required_admission_uses_profile_not_supported_true(system, monkeypatch, calibrated, profile, allowed):
    _, _, tools, _ = system
    data = reply(calibrated=calibrated)
    if calibrated:
        data["admission"]["profile_id"] = profile
    rag_fixture(monkeypatch, [data])
    job = {"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture", "goal": "Find policy",
           "specification": {"rag_admission_policy": "calibrated_required", "rag_admission_profile_ids": [PROFILE]}}
    if allowed:
        result = await tools.execute(job, {})
        assert result["retrieval"]["supported"] is None
    else:
        with pytest.raises(PermissionError):
            await tools.execute(job, {})


def test_structured_zero_is_evidence_with_complete_provenance(system, monkeypatch):
    client, store, tools, _ = system
    data = reply(sources=[], structured=zero_result())
    data["mode_used"] = "structured"
    calls = rag_fixture(monkeypatch, [data])
    run = submit(client, goal="Count completed orders", steps=[
        {"id": "read", "tool": "rag.search", "args": {"mode": "structured",
            "row_filters": [{"field": "state", "op": "eq", "value": "completed"}], "aggregate": {"operation": "count"}}},
        {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]},
    ])
    worker = Worker(store, tools)
    asyncio.run(worker.once())
    asyncio.run(worker.once())
    final = store.get("a", run)
    assert final["status"] == "completed", final
    result = final["steps"][1]["result"]
    assert result["observations"][0]["value"] == 0 and result["citations"] == []
    assert result["observations"][0]["provenance"]["datasets"][0]["document_id"] == "orders"
    assert calls[0]["aggregate"] == {"operation": "count", "field": None}
    obs = {"read": {"tool": "rag.search", "result": final["steps"][0]["result"]},
           "answer": {"tool": "model.summarize", "result": result}}
    assert evaluate("Count completed orders", obs, "evidence")["accepted"]


@pytest.mark.parametrize("datasets", [[], [{"document_id": f"doc{i}", "revision": "r", "profile_id": "p"} for i in range(21)]])
async def test_zero_without_complete_bounded_provenance_is_rejected(system, monkeypatch, datasets):
    _, _, tools, _ = system
    zero = zero_result()
    zero["provenance"]["datasets"] = datasets
    rag_fixture(monkeypatch, [reply(sources=[], structured=zero)])
    with pytest.raises(ValueError, match="provenance"):
        await tools.execute({"tool": "rag.search", "args": {"mode": "structured"}, "tenant": "a",
                             "run_id": "fixture", "goal": "Count orders"}, {})


def test_semantic_admission_skips_only_query_overlap_veto():
    data = reply(calibrated=True)
    result = {"citations": data["citations"], "retrieval": {key: data[key] for key in (
        "outcome", "supported", "refusal_reason", "admission")}}
    observations = {"read": {"tool": "rag.search", "result": result},
                    "answer": {"tool": "model.summarize", "result": {"outcome": "success",
                                "answer": "Session timeout is 30 minutes [c1]."}}}
    assessment = evaluate("When do credentials lapse?", observations, "evidence")
    assert assessment["accepted"] and assessment["query_overlap"] == 0
    observations["answer"]["result"]["answer"] = "Session timeout is 60 minutes [c1]."
    assert evaluate("When do credentials lapse?", observations, "evidence")["reason"] == "unsupported_numeric_claim"
    observations["answer"]["result"]["answer"] = "Session timeout is 30 minutes [foreign]."
    assert evaluate("When do credentials lapse?", observations, "evidence")["reason"] == "unknown_citation"
    observations["answer"]["result"] = refusal_result("model_rejected", stop_reason="model_refused")
    assert not evaluate("When do credentials lapse?", observations, "evidence")["accepted"]


async def test_calibrated_answerability_obeys_profile_and_threshold(system, monkeypatch):
    _, _, tools, _ = system
    accepted = reply(calibrated=True)
    accepted["admission"]["basis"] = "calibrated_answerability"
    accepted["supported"] = True
    refused = reply(reason="below_admission_threshold")
    refused["admission"]["basis"] = "calibrated_answerability"
    calls = rag_fixture(monkeypatch, [accepted, refused])
    job = {"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture", "goal": "Find policy",
           "specification": {"rag_admission_policy": "calibrated_required", "rag_admission_profile_ids": [PROFILE]}}
    first = await tools.execute(job, {})
    assert first["retrieval"]["supported"] is True and first["citations"]
    second = await tools.execute(job, {})
    assert second["retrieval"]["refusal_reason"] == "below_admission_threshold" and second["citations"] == []
    assert len(calls) == 2


async def test_answerability_acceptance_requires_explicit_support(system, monkeypatch):
    _, _, tools, _ = system
    data = reply(calibrated=True)
    data["admission"]["basis"] = "calibrated_answerability"
    rag_fixture(monkeypatch, [data])
    with pytest.raises(ValueError, match="supported result"):
        await tools.execute({"tool": "rag.search", "args": {}, "tenant": "a", "run_id": "fixture",
                             "goal": "Find policy"}, {})


def test_reported_upstream_identity_is_bounded_and_event_safe(system, monkeypatch):
    client, store, tools, _ = system
    data = reply()
    data["mode_used"] = "lightrag"
    data["engine"] = "lightrag:authorized-evidence-v1"
    implementation = {"library": "lightrag-hku", "version": "1.5.7", "source_commit": "a" * 40,
                      "adapter_version": "v03"}
    data["trace"] = [{"operation": "external_backend", "backend": "lightrag", "implementation": implementation,
                      "diagnostics": {"source_count": 1, "engine_executed": True,
                                      "private_provider_message": "private trace never retained"},
                      "query": "private query never retained", "source": SOURCE["text"]}]
    rag_fixture(monkeypatch, [data])
    run = submit(client, steps=[{"id": "read", "tool": "rag.search", "args": {"mode": "lightrag"}}])
    asyncio.run(Worker(store, tools).once())
    result = store.get("a", run)["steps"][0]["result"]
    assert result["upstream"] == {"engine": "lightrag", "implementation": implementation,
                                  "source_count": 1, "engine_executed": True}
    assert "trace" not in result and "private" not in json.dumps(result)
    events = store.events("a", run)
    assert any(event["payload"].get("upstream_engine") == "lightrag" for event in events)
    assert all(value not in json.dumps(events) for value in (SOURCE["text"], "session", "private", "a" * 40))


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(mode_used="raptor"),
    lambda data: data["trace"][0]["implementation"].update(source_commit="unbounded arbitrary metadata"),
    lambda data: data["trace"][0]["diagnostics"].update(engine_executed="yes"),
])
async def test_malformed_upstream_identity_is_not_retained(system, monkeypatch, mutation):
    _, _, tools, _ = system
    data = reply()
    data["mode_used"] = "lightrag"
    data["trace"] = [{"operation": "external_backend", "backend": "lightrag",
                      "implementation": {"library": "lightrag-hku", "version": "1.5.7", "source_commit": "a" * 40,
                                         "adapter_version": "v03"},
                      "diagnostics": {"source_count": 1, "engine_executed": True}}]
    mutation(data)
    rag_fixture(monkeypatch, [data])
    with pytest.raises(ValueError):
        await tools.execute({"tool": "rag.search", "args": {"mode": "lightrag"}, "tenant": "a",
                             "run_id": "fixture", "goal": "Find policy"}, {})


@pytest.mark.parametrize("policy", ["compatibility", "calibrated_required"])
def test_active_legacy_finished_success_is_reassessed_without_tool_replay(system, monkeypatch, policy):
    client, store, tools, _ = system
    run = submit(client, runtime_mode="agent_loop", max_rounds=1)
    job = store.claim()
    legacy_result = {"outcome": "success", "answer": "Session timeout is 30 minutes [c1].", "citations": [SOURCE]}
    state = {"version": 1, "finished": True, "result": legacy_result, "round": 1, "cursor": 2,
             "plan": [{"id": "read", "tool": "rag.search"},
                      {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]}],
             "observations": {"read": {"tool": "rag.search", "result": {"supported": False, "citations": [SOURCE]}},
                              "answer": {"tool": "model.summarize", "result": legacy_result}},
             "evaluation": {"accepted": True}, "evaluations": [], "memory": {"citations": []}}
    assert store.checkpoint(job, state)
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run,))
    recovered = store.claim()
    recovered["specification"].update(rag_admission_policy=policy, rag_admission_profile_ids=[PROFILE])

    async def forbidden(*args, **kwargs):
        raise AssertionError("Committed tool observations must not be replayed")

    monkeypatch.setattr(tools, "execute", forbidden)
    result = asyncio.run(AgentRuntime(store, tools).run(recovered))
    assert result["outcome"] == "insufficient_evidence" and result["stop_reason"] == "rounds_exhausted"
    assert result["citations"] == [] and result["last_refusal_reason"] == "model_rejected"
    assert store.complete(recovered, result) and not store.complete(job, legacy_result)
    assert store.get("a", run)["status"] == "failed"


def test_refusal_round_resumes_without_retrieval_replay_and_preserves_events(system, monkeypatch):
    client, store, tools, config = system
    calls = rag_fixture(monkeypatch, [reply(reason="model_rejected"), reply(calibrated=True)])
    run = submit(client, runtime_mode="agent_loop", max_rounds=2)
    job = store.claim()
    original = tools.execute

    class Crash(BaseException):
        pass

    async def crash_after_retrieval(nested, inputs):
        if nested["tool"] == "model.summarize":
            raise Crash()
        return await original(nested, inputs)

    monkeypatch.setattr(tools, "execute", crash_after_retrieval)
    with pytest.raises(Crash):
        asyncio.run(AgentRuntime(store, tools).run(job))
    assert len(calls) == 1
    with store.connect() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE run_id=?", (run,))
    recovered = store.claim()
    monkeypatch.setattr(tools, "execute", original)
    result = asyncio.run(AgentRuntime(store, tools).run(recovered))
    assert result["outcome"] == "success" and result["rounds"] == 2 and len(calls) == 2
    assert store.complete(recovered, result) and not store.complete(job, result)
    events = store.events("a", run)
    assert any(event["payload"].get("refusal_reason") == "model_rejected" for event in events)
    rendered = json.dumps(events)
    for private in (SOURCE["text"], "What is the session timeout?", "rag-a"):
        assert private not in rendered
    reopened = Store(config.db_path)
    assert reopened.get("a", run)["status"] == "completed"


def test_refusal_round_exhaustion_retains_typed_last_reason(system, monkeypatch):
    client, store, tools, _ = system
    calls = rag_fixture(monkeypatch, [reply(reason="below_admission_threshold")])
    run = submit(client, runtime_mode="agent_loop", max_rounds=2)
    asyncio.run(Worker(store, tools).once())
    state = store.get("a", run)
    assert state["status"] == "failed"
    result = state["steps"][0]["result"]
    assert result["stop_reason"] == "rounds_exhausted"
    assert result["last_refusal_reason"] == "below_admission_threshold"
    assert result["citations"] == [] and len(calls) == 2
