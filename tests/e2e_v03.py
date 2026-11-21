"""Real RAG HTTP -> independent Agent worker -> terminal HTTP/SSE/stdio MCP.

Optional AGENT_E2E_RAG_URL/KEY/MODE/QUESTION/DOCUMENT_IDS_JSON reuses an already
running research RAG; that mode never ingests, deletes, or stops its corpus/server.
The caller must provide a disposable PostgreSQL URL if selecting that backend.
"""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
QUESTION = "What is the session timeout?"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def decoded(result):
    assert not result.isError, result
    value = result.structuredContent
    if value is None:
        text = next((item.text for item in result.content if item.type == "text"), None)
        assert text is not None
        value = json.loads(text)
    return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value


async def mcp_inspect(env, workflow_id, expected, question, mode, *, start=False):
    for module in ("rags.mcp_server", "agentic.mcp_server"):
        params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                assert (await session.initialize()).serverInfo.name
                names = {tool.name for tool in (await session.list_tools()).tools}
                if module.startswith("rags"):
                    assert "search_knowledge" in names
                    data = decoded(await session.call_tool("search_knowledge", {"question": question, "mode": mode}))
                    assert data["outcome"] == expected
                    assert bool(data["citations"]) is (expected == "success")
                else:
                    assert {"start_research_workflow", "get_workflow_status", "get_workflow_events"} <= names
                    if start:
                        created = decoded(await session.call_tool("start_research_workflow", {"goal": question}))
                        workflow_id = created["id"]
                    deadline = time.monotonic() + 180
                    while True:
                        state = decoded(await session.call_tool("get_workflow_status", {"workflow_id": workflow_id}))
                        if state["status"] in {"completed", "failed", "cancelled"}:
                            break
                        assert time.monotonic() < deadline, state
                        await asyncio.sleep(0.1)
                    assert state["status"] == ("completed" if expected == "success" else "failed"), state
                    assert state["steps"][-1]["result"]["outcome"] == expected, state
                    events = decoded(await session.call_tool("get_workflow_events", {"workflow_id": workflow_id}))
                    assert isinstance(events, list) and events
                    assert events[-1]["payload"]["outcome"] == expected
                    assert question not in json.dumps(events)


def main():
    external = os.getenv("AGENT_E2E_RAG_URL")
    rag_mode = os.getenv("AGENT_E2E_RAG_MODE", "hybrid")
    question = os.getenv("AGENT_E2E_RAG_QUESTION", QUESTION)
    allowed_documents = json.loads(os.getenv("AGENT_E2E_RAG_DOCUMENT_IDS_JSON", "[]"))
    if external and (not isinstance(allowed_documents, list) or not allowed_documents):
        raise ValueError("External RAG verification requires its authorized fixture document IDs")
    profile_file = os.getenv("AGENT_E2E_ADMISSION_PROFILE")
    postgres = os.getenv("AGENT_E2E_POSTGRES_URL")
    processes, checks = [], []
    with tempfile.TemporaryDirectory(prefix="agent-v03-e2e-") as directory, ExitStack() as stack:
        agent_url = f"http://127.0.0.1:{free_port()}"
        rag_url = external.rstrip("/") if external else f"http://127.0.0.1:{free_port()}"
        rag_key = os.getenv("AGENT_E2E_RAG_KEY", "disposable-rag-key")
        tenant = "e3-" + uuid.uuid4().hex[:12]
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENT_", "RAGS_"))}
        env.update({
            "RAGS_ENV": "test", "RAGS_DB_PATH": directory + "/rags.db",
            "RAGS_KEYS_JSON": json.dumps({rag_key: tenant}),
            "RAGS_MODEL_PROVIDER": "extractive", "RAGS_EMBEDDING_PROVIDER": "local",
            "RAGS_ADMISSION_POLICY": "compatibility", "RAGS_URL": rag_url, "RAGS_API_KEY": rag_key,
            "AGENT_ENV": "test", "AGENT_DB_PATH": directory + "/agent.db",
            "AGENT_KEYS_JSON": json.dumps({"disposable-agent-key": tenant}),
            "AGENT_RAG_KEYS_JSON": json.dumps({tenant: rag_key}),
            "AGENT_APPROVER_KEYS_JSON": json.dumps({"disposable-approver-key": tenant}),
            "AGENT_MODEL_PROVIDER": "offline", "AGENT_RAG_URL": rag_url,
            "AGENT_ARTIFACT_DIR": directory + "/artifacts", "AGENT_RATE_DB_PATH": directory + "/limits.db",
            "AGENT_RATE_PER_MINUTE": "3000", "AGENT_URL": agent_url, "AGENT_API_KEY": "disposable-agent-key",
            "AGENT_RAG_ADMISSION_POLICY": "compatibility",
        })
        if postgres:
            env["AGENT_DATABASE_URL"] = postgres
        if profile_file:
            profile = json.loads(Path(profile_file).read_text())
            env.update({"RAGS_ADMISSION_POLICY": "calibrated_required", "RAGS_ADMISSION_PROFILE": profile_file,
                        "AGENT_RAG_ADMISSION_POLICY": "calibrated_required",
                        "AGENT_RAG_ADMISSION_PROFILE_IDS_JSON": json.dumps([profile["profile_id"]])})
        commands = []
        if not external:
            commands.append(("rag", [sys.executable, "-m", "uvicorn", "rags.api:app", "--host", "127.0.0.1",
                                      "--port", rag_url.rsplit(":", 1)[1]]))
        commands.extend([
            ("api", [sys.executable, "-m", "uvicorn", "agentic.api:app", "--host", "127.0.0.1",
                     "--port", agent_url.rsplit(":", 1)[1]]),
            ("worker", [sys.executable, "-m", "agentic.worker"]),
        ])
        try:
            for name, command in commands:
                log = stack.enter_context(open(Path(directory) / (name + ".log"), "w+"))
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
                processes.append((name, process, log))
            client = stack.enter_context(httpx.Client(timeout=180, trust_env=False))
            for base in (rag_url, agent_url):
                deadline = time.monotonic() + 90
                while True:
                    try:
                        if client.get(base + "/live", timeout=2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "API readiness deadline"
                    time.sleep(0.1)

            def request(method, base, path, *, data=None, expected=200):
                token = rag_key if base == rag_url else "disposable-agent-key"
                response = client.request(method, base + path, headers={"Authorization": "Bearer " + token}, json=data)
                assert response.status_code == expected, (path, response.status_code, response.text[:1000])
                return response.json()

            def submit(goal, **payload):
                return request("POST", agent_url, "/workflows", data={"goal": goal, **payload}, expected=202)["id"]

            def finish(run_id):
                deadline = time.monotonic() + 180
                while True:
                    value = request("GET", agent_url, "/workflows/" + run_id)
                    if value["status"] in {"completed", "failed", "cancelled"}:
                        return value
                    assert time.monotonic() < deadline, value
                    time.sleep(0.1)

            def inspect_stream(run_id, expected):
                response = client.get(agent_url + f"/workflows/{run_id}/events/stream",
                                      headers={"Authorization": "Bearer disposable-agent-key"})
                assert response.status_code == 200 and "event: done" in response.text
                assert expected in response.text

            if not external:
                empty = request("POST", rag_url, "/query", data={"question": question, "generate": False})
                assert empty["outcome"] == "insufficient_evidence" and empty["answer"] is None
                assert empty["refusal_reason"] == "no_candidates" and empty["citations"] == []
                no_answer = submit(question)
                state = finish(no_answer)
                assert state["status"] == "failed", state
                assert state["steps"][-1]["result"]["outcome"] == "insufficient_evidence"
                assert state["steps"][-1]["attempts"] == 1
                assert not state["usage"]["calls"]
                inspect_stream(no_answer, "insufficient_evidence")
                asyncio.run(mcp_inspect(env, no_answer, "insufficient_evidence", question, "hybrid", start=True))
                exhausted = finish(submit(question, runtime_mode="agent_loop", max_rounds=2))
                assert exhausted["status"] == "failed", exhausted
                result = exhausted["steps"][0]["result"]
                assert result["stop_reason"] == "rounds_exhausted" and result["last_refusal_reason"] == "no_candidates"
                checks.append("Actual empty RAG -> failed DAG/loop, no generation, terminal REST/SSE/both MCP refusal")
                request("POST", rag_url, "/documents", expected=201, data={
                    "document_id": "session-v03", "title": "Session policy",
                    "text": "# Session\nSession timeout is 30 minutes after inactivity. Refresh tokens rotate after login.",
                })
                allowed_documents = ["session-v03"]

            run = submit(question, steps=[
                {"id": "read", "tool": "rag.search", "args": {"mode": rag_mode}},
                {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]},
            ])
            state = finish(run)
            assert state["status"] == "completed", state
            retrieved, summarized = (step["result"] for step in state["steps"])
            assert retrieved["retrieval"]["outcome"] == summarized["outcome"] == "success"
            assert retrieved["requested_strategy"] == retrieved["strategy"] == rag_mode
            originals = {source["chunk_id"] for source in retrieved["citations"]}
            assert summarized["citations"] and {source["chunk_id"] for source in summarized["citations"]} <= originals
            assert all(source["document_id"] in allowed_documents for source in retrieved["citations"])
            if external:
                assert retrieved["engine"] == rag_mode + ":authorized-evidence-v1"
                upstream = retrieved["upstream"]
                assert upstream["engine"] == rag_mode and upstream["engine_executed"]
                assert upstream["source_count"] > 0 and len(upstream["implementation"]["source_commit"]) == 40
                assert "trace" not in retrieved
                checks.append("Actual upstream " + rag_mode + " identity and authorized source lineage survive independent Agent worker")
            events = request("GET", agent_url, f"/workflows/{run}/events")
            for event in events:
                assert not ({"query", "question", "text", "document_id", "trace", "diagnostics", "implementation"}
                            & event["payload"].keys())
            inspect_stream(run, "success")
            asyncio.run(mcp_inspect(env, run, "success", question, rag_mode, start=not external))
            checks.append("Actual positive answer -> terminal REST/SSE/both MCP; source IDs and safe events")

            if not external:
                mixed = finish(submit(question, steps=[
                    {"id": "empty", "tool": "rag.search", "args": {"mode": "sparse", "question": "xyzzyquuxnocorpus"}},
                    {"id": "good", "tool": "rag.search"},
                    {"id": "merge", "tool": "data.merge", "depends_on": ["empty", "good"]},
                    {"id": "answer", "tool": "model.summarize", "depends_on": ["merge"]},
                ]))
                assert mixed["status"] == "completed", mixed
                assert mixed["steps"][0]["result"]["retrieval"]["outcome"] == "insufficient_evidence"
                request("POST", rag_url, "/documents", expected=201, data={
                    "document_id": "orders-v03", "title": "Orders", "rows": [{"state": "pending", "amount": 3}],
                })
                zero = finish(submit("Count completed orders", steps=[
                    {"id": "read", "tool": "rag.search", "args": {"mode": "structured",
                        "row_filters": [{"field": "state", "op": "eq", "value": "completed"}],
                        "aggregate": {"operation": "count"}}},
                    {"id": "answer", "tool": "model.summarize", "depends_on": ["read"]},
                ]))
                assert zero["status"] == "completed", zero
                observation = zero["steps"][-1]["result"]["observations"][0]
                assert observation["value"] == 0 and observation["provenance"]["datasets"]
                exact_zero = finish(submit("Calculate 2 - 2"))
                assert exact_zero["status"] == "completed" and exact_zero["steps"][0]["result"]["value"] == 0
                checks.append("Actual refused+accepted branch merge, authorized structured zero and arithmetic zero")
            print(json.dumps({"status": "passed", "checks": checks, "live_paid_provider": False,
                              "required_admission": bool(profile_file), "external_research_rag": bool(external)}, indent=2))
        except BaseException:
            for name, process, log in processes:
                log.flush()
                log.seek(0)
                print(name, "exit=", process.poll(), log.read()[-4000:], file=sys.stderr)
            raise
        finally:
            for _, process, _ in processes:
                process.terminate()
            for _, process, _ in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
