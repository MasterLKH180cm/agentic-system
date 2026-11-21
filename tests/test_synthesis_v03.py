"""Real local provider HTTP verifies the synthesis protocol, not model quality."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn
from agentic.store import Store
from agentic.tools import Tools
from agentic.worker import Worker


SOURCE = {"chunk_id": "c1", "document_id": "session", "text": "Session timeout is 30 minutes."}


def answer(text="Session timeout is 30 minutes [c1].", *, outcome="success", cited_ids=None, reason=None):
    return json.dumps({"outcome": outcome, "answer": text,
                       "cited_ids": ["c1"] if cited_ids is None else cited_ids, "refusal_reason": reason})


@pytest.fixture
def system(tmp_path, monkeypatch):
    state = {"responses": [], "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer fixture-provider-key"
            size = int(self.headers["Content-Length"])
            assert size <= 100_000
            payload = json.loads(self.rfile.read(size))
            state["calls"].append(payload)
            text = state["responses"].pop(0)
            body = json.dumps({"choices": [{"message": {"content": text}}],
                               "usage": {"prompt_tokens": 120, "completion_tokens": 50}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "model.db"))
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"a-key":"a"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"rag-a"}')
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider-key")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("AGENT_MAX_MODEL_OUTPUT_TOKENS", "512")
    monkeypatch.setenv("AGENT_MAX_MODEL_TOKENS", "6000")
    monkeypatch.setenv("AGENT_RAG_ADMISSION_POLICY", "compatibility")
    monkeypatch.setenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", "[]")
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    config = Settings.from_env()
    store = Store(config.db_path)
    tools = Tools(config, ModelGateway(config, store), store)
    try:
        yield store, tools, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def prepare(store, *, sources=None, goal="What is the session timeout?", budget=6000):
    workflow = WorkflowIn(goal=goal, steps=[Step(id="read", tool="rag.search"),
                                           Step(id="answer", tool="model.summarize", depends_on=["read"])])
    run = store.submit("a", workflow, specification={"max_model_tokens": budget,
        "rag_admission_policy": "compatibility", "rag_admission_profile_ids": []})["id"]
    job = store.claim()
    assert job["id"] == "read" and job["run_id"] == run
    assert store.complete(job, {"citations": [SOURCE] if sources is None else sources})
    return run


def test_model_refusal_is_persisted_once_without_generic_retry(system):
    store, tools, state = system
    state["responses"].append(answer("private provider explanation", outcome="insufficient_evidence",
                                     cited_ids=[], reason="model_rejected"))
    run = prepare(store)
    worker = Worker(store, tools)
    assert asyncio.run(worker.once()) == 1
    assert asyncio.run(worker.once()) == 0
    final = store.get("a", run)
    assert final["status"] == "failed" and len(state["calls"]) == 1
    step = final["steps"][1]
    assert step["attempts"] == 1 and step["error"] == "insufficient_evidence"
    assert step["result"]["refusal_reason"] == "model_rejected" and step["result"]["citations"] == []
    assert "private provider explanation" not in json.dumps(final)
    assert "private provider explanation" not in json.dumps(store.events("a", run))
    usage = store.usage("a", run)
    assert len(usage["calls"]) == 1 and usage["calls"][0]["status"] == "reported"


def test_full_json_prompt_schema_framing_and_output_reserve_fit_budget(system):
    store, tools, state = system
    goal = '請問 session timeout？\n包含 "引號" 與反斜線 \\ 的問題。'
    source = {**SOURCE, "text": SOURCE["text"] + ' 漢字\n"引用"\\字元 ' * 900}
    state["responses"].append(answer())
    run = prepare(store, sources=[source], goal=goal, budget=3000)
    assert asyncio.run(Worker(store, tools).once()) == 1
    final = store.get("a", run)
    assert final["status"] == "completed", final
    payload = state["calls"][0]
    system_prompt, user_prompt = (item["content"] for item in payload["messages"])
    packed = json.loads(user_prompt)
    assert packed["question"] == goal and packed["sources"]
    assert "cited_ids" in system_prompt and "refusal_reason" in system_prompt
    assert ModelGateway.input_units(system_prompt, user_prompt) + payload["max_completion_tokens"] <= 3000
    assert payload["max_completion_tokens"] == 512
    assert final["steps"][1]["result"]["citations"][0]["text_truncated"]


@pytest.mark.parametrize("text", [
    "not-json",
    answer("Session timeout is 30 minutes."),
    answer("Session timeout is 30 minutes [foreign].", cited_ids=["foreign"]),
    answer("Session timeout is 60 minutes [c1]."),
    answer(outcome="insufficient_evidence", reason="model_rejected"),
    answer("x" * 12001),
    '{"outcome":"success","outcome":"insufficient_evidence","answer":"","cited_ids":[],"refusal_reason":"model_rejected"}',
    json.dumps({"outcome": "success", "answer": "30 [c1]", "cited_ids": ["c1"], "refusal_reason": None, "extra": True}),
])
async def test_invalid_model_envelopes_never_become_success(system, text):
    store, tools, state = system
    state["responses"].append(text)
    prepare(store)
    job = store.claim()
    with pytest.raises(ValueError):
        await tools.execute(job, store.input_results(job["run_id"], job["depends_on"]))
    assert len(state["calls"]) == 1


def test_no_usable_evidence_and_unfit_context_do_not_call_or_reserve_model(system):
    store, tools, state = system
    run = prepare(store, sources=[])
    asyncio.run(Worker(store, tools).once())
    assert store.get("a", run)["steps"][1]["result"]["refusal_reason"] == "no_candidates"
    assert not store.usage("a", run)["calls"] and not state["calls"]
    tiny = prepare(store, budget=500)
    asyncio.run(Worker(store, tools).once())
    assert store.get("a", tiny)["steps"][1]["result"]["refusal_reason"] == "context_budget_exhausted"
    assert not store.usage("a", tiny)["calls"] and not state["calls"]


def test_already_exhausted_execution_budget_is_not_relabelled_as_refusal(system):
    store, tools, state = system
    run = prepare(store, budget=6000)
    store.reserve_usage("a", run, "fixture", "planning", 6000, None, default_limit=6000)
    asyncio.run(Worker(store, tools).once())
    result = store.get("a", run)["steps"][1]["result"]
    assert result["outcome"] == "budget_exhausted" and not state["calls"]
