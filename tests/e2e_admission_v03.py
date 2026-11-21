"""Actual frozen Qwen admission -> HTTP RAG -> independent Agent worker -> MCP.

Two frozen calibration examples exercise the integration contract only. This
does not measure quality or choose a model, prompt, profile, or threshold. Run
only after any separate Qwen verifier has released its model memory.
"""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import rags
from rags.admission import canonical_digest, load_profile
from rags.admission_eval import read_dataset, write_new

from e2e_v03 import decoded, free_port


ROOT = Path(__file__).resolve().parents[1]
RAG_ROOT = Path(rags.__file__).resolve().parents[1]
CALIBRATION_CASES = (
    ("cal-en-apiary-positive-literal", "success"),
    ("cal-en-apiary-negative-missing-field", "insufficient_evidence"),
)


def check_assessment(data, expected, profile, source_ids):
    assert data["outcome"] == expected, "Unexpected evidence outcome"
    gate = data["admission"]
    assert gate["policy_version"] == "evidence-admission-v1"
    assert gate["basis"] == "calibrated_answerability"
    assert gate["profile_id"] == profile["profile_id"]
    assert gate["threshold"] == profile["threshold"]
    assert type(gate["score"]) in (int, float) and math.isfinite(gate["score"])
    assert gate["candidate_count"] == 1
    accepted = expected == "success"
    assert gate["decision"] == ("accept" if accepted else "abstain")
    assert gate["accepted_count"] == int(accepted)
    assert (gate["score"] >= gate["threshold"]) is accepted
    assert data["supported"] is accepted
    assert data["refusal_reason"] == (None if accepted else "below_admission_threshold")
    if "citations" in data:
        assert {source["chunk_id"] for source in data["citations"]} == (set(source_ids) if accepted else set())


def check_events(events, *, question, passage, document_id, credentials):
    assert events, "Missing durable workflow events"
    forbidden = {"query", "question", "text", "answer", "document_id", "revision", "trace", "diagnostics", "raw_response"}

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden & value.keys(), "Content appeared in an event payload"
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for event in events:
        inspect(event["payload"])
    encoded = json.dumps(events)
    assert all(private not in encoded for private in (question, passage, document_id, *credentials))


async def check_mcp(env, workflow_id, expected, question, profile, source_ids):
    for module in ("rags.mcp_server", "agentic.mcp_server"):
        params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                assert (await session.initialize()).serverInfo.name
                names = {tool.name for tool in (await session.list_tools()).tools}
                if module == "rags.mcp_server":
                    assert "search_knowledge" in names
                    data = decoded(await session.call_tool("search_knowledge", {"question": question, "top_k": 1}))
                    check_assessment(data, expected, profile, source_ids)
                else:
                    assert {"get_workflow_status", "get_workflow_events"} <= names
                    state = decoded(await session.call_tool("get_workflow_status", {"workflow_id": workflow_id}))
                    assert state["status"] == ("completed" if expected == "success" else "failed")
                    assert state["steps"][-1]["result"]["outcome"] == expected
                    check_assessment(state["steps"][0]["result"]["retrieval"], expected, profile, source_ids)
                    events = decoded(await session.call_tool("get_workflow_events", {"workflow_id": workflow_id}))
                    assert events[-1]["payload"]["outcome"] == expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag-python", required=True, help="Python with the frozen Qwen runtime and RAG API installed")
    parser.add_argument("--profile", required=True, help="Existing immutable Qwen admission profile")
    parser.add_argument("--model-cache", required=True, help="Existing pinned checkpoint cache; no model download")
    parser.add_argument("--report", required=True, help="New JSON report; existing files are never overwritten")
    parser.add_argument("--calibration", default=str(RAG_ROOT / "examples/admission_calibration_v03.json"))
    args = parser.parse_args()
    report_path = Path(args.report).resolve()
    if report_path.exists():
        parser.error("Refusing to overwrite an existing integration report")
    # Preserve a virtualenv interpreter symlink: resolving it would launch the
    # base Python without the profile's pinned environment.
    rag_python = Path(args.rag_python).expanduser().absolute()
    profile_path, model_cache = (Path(value).expanduser().resolve() for value in (args.profile, args.model_cache))
    if not rag_python.is_file() or not model_cache.is_dir():
        parser.error("The RAG Python executable and existing model cache are required")
    # Loading this JSON and checking its identity does not instantiate a scorer.
    profile = load_profile(str(profile_path))
    if profile["scorer"]["model"] != "Qwen/Qwen3-1.7B":
        parser.error("This integration gate requires the frozen Qwen answerability profile")
    dataset = read_dataset(args.calibration)
    if dataset["split"] != "calibration" or canonical_digest(dataset["cases"]) != profile["calibration"]["cases_digest"]:
        parser.error("The calibration fixture must match the frozen profile")
    cases = {case["id"]: case for case in dataset["cases"]}
    for case_id, expected in CALIBRATION_CASES:
        case = cases[case_id]
        assert case["answerable"] is (expected == "success") and len(case["passages"]) == 1
    started = time.monotonic()
    report = {"schema_version": "agent-admission-http-verification-v1", "status": "running",
              "measured_at": datetime.now(timezone.utc).isoformat(), "profile_id": profile["profile_id"],
              "profile_file_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
              "basis": "calibrated_answerability", "frozen_threshold": profile["threshold"],
              "calibration_cases_digest": dataset["cases_digest"]["value"], "cases": [],
              "scope": "Two frozen calibration cases exercise the integration contract; no quality, calibration, or model-selection claim",
              "admission": "actual pinned local Qwen inference with verified profile/runtime/artifact identity",
              "synthesis": "explicit deterministic local HTTP fixture, not a live generation model",
              "live_paid_provider": False}
    processes = []
    calls = []
    fixture_key = "fixture-" + uuid.uuid4().hex

    class SynthesisFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer " + fixture_key
            length = int(self.headers["Content-Length"])
            assert 0 < length < 100_000
            payload = json.loads(self.rfile.read(length))
            evidence = json.loads(payload["messages"][-1]["content"])
            sources = evidence["sources"]
            assert sources, "A refused query must never reach the synthesis provider"
            # Extract only the actual admitted prompt evidence. No case labels,
            # expected answers or calibration scores are used by this fixture.
            source = sources[0]
            answer = source["text"].replace("[", "(").replace("]", ")") + f" [{source['chunk_id']}]"
            calls.append({"source_ids": [source["chunk_id"]]})
            content = json.dumps({"outcome": "success", "answer": answer,
                                  "cited_ids": [source["chunk_id"]], "refusal_reason": None})
            body = json.dumps({"choices": [{"message": {"content": content}}],
                               "usage": {"prompt_tokens": 120, "completion_tokens": 100}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *items):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SynthesisFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="agent-admission-http-") as directory, ExitStack() as stack:
            rag_port, agent_port = free_port(), free_port()
            while agent_port == rag_port:
                agent_port = free_port()
            rag_url, agent_url = f"http://127.0.0.1:{rag_port}", f"http://127.0.0.1:{agent_port}"
            tenants = ["admission-" + uuid.uuid4().hex[:12] for _ in CALIBRATION_CASES]
            rag_keys = ["rag-" + uuid.uuid4().hex for _ in tenants]
            agent_keys = ["agent-" + uuid.uuid4().hex for _ in tenants]
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENT_", "RAGS_", "OPENAI_", "ANTHROPIC_"))}
            env.update({
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                "RAGS_ENV": "test", "RAGS_DB_PATH": directory + "/rag.db",
                "RAGS_KEYS_JSON": json.dumps(dict(zip(rag_keys, tenants))),
                "RAGS_MODEL_PROVIDER": "extractive", "RAGS_EMBEDDING_PROVIDER": "local",
                "RAGS_ADMISSION_POLICY": "calibrated_required", "RAGS_ADMISSION_PROFILE": str(profile_path),
                "RAGS_MODEL_CACHE": str(model_cache), "RAGS_URL": rag_url,
                "AGENT_ENV": "test", "AGENT_DB_PATH": directory + "/agent.db",
                "AGENT_KEYS_JSON": json.dumps(dict(zip(agent_keys, tenants))),
                "AGENT_RAG_KEYS_JSON": json.dumps(dict(zip(tenants, rag_keys))),
                "AGENT_MODEL_PROVIDER": "offline", "AGENT_RAG_URL": rag_url, "AGENT_URL": agent_url,
                "AGENT_RAG_ADMISSION_POLICY": "calibrated_required",
                "AGENT_RAG_ADMISSION_PROFILE_IDS_JSON": json.dumps([profile["profile_id"]]),
                "AGENT_RAG_TIMEOUT_SECONDS": "180", "AGENT_STEP_TIMEOUT_SECONDS": "240",
                "AGENT_ARTIFACT_DIR": directory + "/artifacts", "AGENT_RATE_DB_PATH": directory + "/rate.db",
                "AGENT_RATE_PER_MINUTE": "3000", "FIXTURE_SYNTHESIS_KEY": fixture_key,
                "AGENT_MODEL_ROUTES_JSON": json.dumps([{"name": "local-synthesis-fixture", "provider": "openai",
                    "model": "explicit-contract-fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key_env": "FIXTURE_SYNTHESIS_KEY", "purposes": ["synthesis"]}]),
            })
            commands = [
                ("rag", [str(rag_python), "-m", "uvicorn", "rags.api:app", "--host", "127.0.0.1", "--port", str(rag_port)], RAG_ROOT),
                ("agent-api", [sys.executable, "-m", "uvicorn", "agentic.api:app", "--host", "127.0.0.1", "--port", str(agent_port)], ROOT),
                ("worker", [sys.executable, "-m", "agentic.worker"], ROOT),
            ]
            try:
                for name, command, cwd in commands:
                    log = stack.enter_context(open(Path(directory) / (name + ".log"), "w+"))
                    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log)
                    processes.append((name, process, log))
                client = stack.enter_context(httpx.Client(timeout=180, trust_env=False))
                for url in (rag_url, agent_url):
                    deadline = time.monotonic() + 90
                    while True:
                        assert all(process.poll() is None for _, process, _ in processes), "A service exited before readiness"
                        try:
                            if client.get(url + "/live", timeout=2).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline, "Service readiness deadline"
                        time.sleep(0.1)

                def request(method, url, path, key, data=None, expected=200):
                    response = client.request(method, url + path, headers={"Authorization": "Bearer " + key}, json=data)
                    assert response.status_code == expected, f"{path} returned HTTP {response.status_code}"
                    return response.json()

                for index, (case_id, expected) in enumerate(CALIBRATION_CASES):
                    case = cases[case_id]
                    question, passage = case["question"], case["passages"][0]["text"]
                    document_id = "source-" + uuid.uuid4().hex
                    rag_key, agent_key = rag_keys[index], agent_keys[index]
                    print(f"Actual-profile HTTP contract: {case_id}", file=sys.stderr, flush=True)
                    ingested = request("POST", rag_url, "/documents", rag_key, expected=201, data={
                        "document_id": document_id, "title": "Original source", "text": passage,
                        "chunking": {"strategy": "fixed", "max_tokens": 512, "overlap_tokens": 0},
                        "tokenizer": {"backend": "unicode"},
                    })
                    assert ingested["chunks"] == 1 and ingested["derived_nodes"] == 0
                    direct = request("POST", rag_url, "/query", rag_key,
                                     data={"question": question, "mode": "hybrid", "top_k": 1, "generate": False})
                    assert direct["answer"] is None
                    source_ids = direct["retrieval_diagnostics"]["candidate_ids"]
                    assert len(source_ids) == 1
                    check_assessment(direct, expected, profile, source_ids)
                    if expected == "success":
                        source = direct["citations"][0]
                        assert source["text"] == passage and source["document_id"] == document_id
                        assert source["revision"] == ingested["revision"] and source["profile_id"] == ingested["profile_id"]

                    calls_before = len(calls)
                    created = request("POST", agent_url, "/workflows", agent_key, expected=202, data={
                        "goal": question, "steps": [{"id": "read", "tool": "rag.search", "args": {"top_k": 1}},
                            {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]}],
                    })
                    run_id = created["id"]
                    deadline = time.monotonic() + 240
                    while True:
                        state = request("GET", agent_url, "/workflows/" + run_id, agent_key)
                        if state["status"] in {"completed", "failed", "cancelled"}:
                            break
                        assert time.monotonic() < deadline, "Workflow completion deadline"
                        time.sleep(0.1)
                    assert state["status"] == ("completed" if expected == "success" else "failed")
                    assert state["specification"]["rag_admission_policy"] == "calibrated_required"
                    assert state["specification"]["rag_admission_profile_ids"] == [profile["profile_id"]]
                    read, answer = state["steps"]
                    check_assessment(read["result"]["retrieval"], expected, profile, source_ids)
                    assert answer["result"]["outcome"] == expected and answer["attempts"] == 1
                    assert len(calls) - calls_before == int(expected == "success")
                    if expected == "success":
                        assert {source["chunk_id"] for source in answer["result"]["citations"]} == set(source_ids)
                        assert len(state["usage"]["calls"]) == 1 and state["usage"]["calls"][0]["status"] == "reported"
                    else:
                        assert answer["result"]["refusal_reason"] == "below_admission_threshold"
                        assert read["result"]["answer"] is None and read["result"]["citations"] == answer["result"]["citations"] == []
                        assert set(read["result"]["quarantined_ids"]) == set(source_ids)
                        assert state["usage"]["calls"] == [] and state["usage"]["charged_tokens"] == 0
                    events = request("GET", agent_url, f"/workflows/{run_id}/events", agent_key)
                    check_events(events, question=question, passage=passage, document_id=document_id,
                                 credentials=[*rag_keys, *agent_keys, fixture_key])
                    assert any(event["payload"].get("admission_profile_id") == profile["profile_id"] for event in events)
                    assert events[-1]["payload"]["outcome"] == expected
                    stream = client.get(agent_url + f"/workflows/{run_id}/events/stream",
                                        headers={"Authorization": "Bearer " + agent_key})
                    assert stream.status_code == 200 and "event: done" in stream.text and expected in stream.text
                    mcp_env = {**env, "RAGS_API_KEY": rag_key, "AGENT_API_KEY": agent_key}
                    asyncio.run(check_mcp(mcp_env, run_id, expected, question, profile, source_ids))
                    report["cases"].append({"case_id": case_id, "outcome": expected, "profile_id": profile["profile_id"],
                        "basis": direct["admission"]["basis"], "score": direct["admission"]["score"],
                        "threshold": direct["admission"]["threshold"], "candidate_source_ids": source_ids,
                        "usable_source_ids": [source["chunk_id"] for source in answer["result"]["citations"]],
                        "agent_status": state["status"], "synthesis_http_calls": len(calls) - calls_before,
                        "model_usage_records": len(state["usage"]["calls"]), "safe_events": True,
                        "terminal_rest_sse_and_both_mcp": True})
                report["status"] = "passed"
            except BaseException:
                for name, process, log in processes:
                    log.flush()
                    log.seek(0)
                    print(name, "exit=", process.poll(), log.read()[-4000:], file=sys.stderr)
                raise
            finally:
                for _, process, _ in processes:
                    if process.poll() is None:
                        process.terminate()
                for _, process, _ in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_new(str(report_path), report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
