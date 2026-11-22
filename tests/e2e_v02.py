"""Disposable real TCP APIs + independent worker + stdio MCP acceptance flow.

Run after installing this repository and its pinned sibling RAGs dependency.
No provider credentials or existing user stores are used.
"""
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from e2e_support import clean_environment


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    checks = []
    processes = []
    with tempfile.TemporaryDirectory(prefix="agentic-v02-e2e-") as directory, ExitStack() as stack:
        rag_port, agent_port = free_port(), free_port()
        rag_url, agent_url = f"http://127.0.0.1:{rag_port}", f"http://127.0.0.1:{agent_port}"
        env = clean_environment()
        hook_secret = "disposable-test-webhook-secret-32-chars"
        env.update({
            "RAGS_ENV": "test", "RAGS_DB_PATH": directory + "/rags.db",
            "RAGS_KEYS_JSON": '{"e2e-rag-a":"a","e2e-rag-b":"b"}',
            "RAGS_EMBEDDING_PROVIDER": "local", "RAGS_MODEL_PROVIDER": "extractive",
            "RAGS_GENERATION_TOKENIZER_JSON": '{"backend":"tiktoken","encoding":"cl100k_base"}',
            "AGENT_ENV": "test", "AGENT_DB_PATH": directory + "/agent.db",
            "AGENT_KEYS_JSON": '{"e2e-agent-a":"a","e2e-agent-b":"b"}',
            "AGENT_APPROVER_KEYS_JSON": '{"e2e-approver-a":"a"}',
            "AGENT_RAG_KEYS_JSON": '{"a":"e2e-rag-a","b":"e2e-rag-b"}',
            "AGENT_RAG_URL": rag_url, "AGENT_MODEL_PROVIDER": "offline",
            "AGENT_ARTIFACT_DIR": directory + "/artifacts", "AGENT_RATE_PER_MINUTE": "3000",
            "AGENT_RATE_DB_PATH": directory + "/limits.db",
            "AGENT_TENANT_POLICIES_JSON": '{"a":{"allowed_namespaces":["default","notes"]}}',
            "AGENT_WEBHOOKS_JSON": json.dumps({"e2e": {"tenant": "a", "secret": hook_secret}}),
            "RAGS_URL": rag_url, "RAGS_API_KEY": "e2e-rag-a",
            "AGENT_URL": agent_url, "AGENT_API_KEY": "e2e-agent-a",
        })
        # Optional CI database must be disposable; the rest of the flow is unchanged.
        if os.getenv("AGENT_E2E_POSTGRES_URL"):
            env["AGENT_DATABASE_URL"] = os.environ["AGENT_E2E_POSTGRES_URL"]
        if os.getenv("RAGS_E2E_POSTGRES_URL"):
            env["RAGS_DATABASE_URL"] = os.environ["RAGS_E2E_POSTGRES_URL"]
        commands = [
            ("rag", [sys.executable, "-m", "uvicorn", "rags.api:app", "--host", "127.0.0.1", "--port", str(rag_port)]),
            ("api", [sys.executable, "-m", "uvicorn", "agentic.api:app", "--host", "127.0.0.1", "--port", str(agent_port)]),
            ("worker", [sys.executable, "-m", "agentic.worker"]),
        ]
        try:
            for name, command in commands:
                logfile = stack.enter_context(open(Path(directory) / (name + ".log"), "w+"))
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=logfile, stderr=logfile)
                processes.append((name, process, logfile))
            client = stack.enter_context(httpx.Client(timeout=15, trust_env=False))
            for base in (rag_url, agent_url):
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if client.get(base + "/live").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise AssertionError("API did not become ready")
                    time.sleep(.1)

            def request(method, base, path, *, token="e2e-agent-a", expected=200, **kwargs):
                response = client.request(method, base + path, headers={"Authorization": "Bearer " + token}, **kwargs)
                assert response.status_code == expected, (method, path, response.status_code, response.text[:2000])
                return response.json()

            def rag(method, path, **kwargs):
                return request(method, rag_url, path, token="e2e-rag-a", **kwargs)

            def agent(method, path, **kwargs):
                return request(method, agent_url, path, **kwargs)

            def finish(run_id, statuses=("completed", "failed", "waiting_approval")):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    state = agent("GET", "/workflows/" + run_id)
                    if state["status"] in statuses:
                        return state
                    time.sleep(.1)
                raise AssertionError(("Workflow deadline", state))

            storage = {"rag": rag("GET", "/health")["storage"],
                       "agent": agent("GET", "/capabilities")["storage"]}
            assert storage["rag"]["backend"] == ("postgresql" if env.get("RAGS_DATABASE_URL") else "sqlite")
            assert storage["agent"]["backend"] == ("postgresql" if env.get("AGENT_DATABASE_URL") else "sqlite")
            if env.get("RAGS_DATABASE_URL"):
                assert storage["rag"]["vector_search"] == "pgvector_exact"
            text = "# Session policy\nSession timeout is 30 minutes after inactivity. Refresh tokens rotate after login.\n" * 8
            doc = rag("POST", "/documents", expected=201, json={
                "document_id": "session", "title": "Session policy", "text": text,
                "tokenizer": {"backend": "tiktoken", "encoding": "cl100k_base"},
                "chunking": {"strategy": "parent_child", "max_tokens": 32, "overlap_tokens": 4, "parent_max_tokens": 96},
                "index_features": ["hierarchy"],
            })
            assert doc["chunks"] > 1
            for mode in ("hybrid", "parent_child", "hierarchical"):
                result = rag("POST", "/query", json={"question": "What is the session timeout?", "mode": mode})
                assert result["mode_used"] == mode and result["citations"]
                assert all(c["text"] == text[c["start_char"]:c["end_char"]] for c in result["citations"])
                assert result["budget"]["input_units"] + result["budget"]["output_reserve"] + result["budget"]["envelope_reserve"] <= result["budget"]["context_budget"]
            foreign = request("POST", rag_url, "/query", token="e2e-rag-b", json={"question": "session timeout"})
            assert not foreign["citations"]
            rebuilt = rag("POST", "/documents/session/reindex", json={"expected_revision": doc["revision"],
                          "chunking": {"strategy": "recursive", "max_tokens": 48, "overlap_tokens": 6}})
            assert rebuilt["profile_id"] != doc["profile_id"]
            rag("POST", "/documents/session/reindex", expected=409, json={"expected_revision": doc["revision"]})
            checks.append("HTTP token-aware ingest, source offsets, hierarchy, tenant isolation, atomic reindex/CAS")

            for start, price in (("2024-01-01", 10), ("2024-06-01", 20)):
                rag("POST", "/documents", expected=201, json={"document_id": "price", "title": "Price",
                    "text": f"The plan costs {price} credits.", "valid_from": start + "T00:00:00Z",
                    "metadata": {"kind": "price"}})
            historic = rag("POST", "/query", json={"question": "plan price", "mode": "temporal",
                "as_of": "2024-03-01T00:00:00Z", "filters": {"metadata": {"kind": "price"}}})
            assert "10 credits" in historic["citations"][0]["text"]
            assert len(rag("GET", "/documents/price/revisions")) == 2
            rag("DELETE", "/documents/price")
            assert not rag("POST", "/query", json={"question": "plan price", "mode": "temporal",
                "as_of": "2024-03-01T00:00:00Z", "filters": {"metadata": {"kind": "price"}}})["citations"]
            rag("POST", "/documents", expected=201, json={"document_id": "sales", "title": "Sales",
                "rows": [{"region": "north", "amount": 10}, {"region": "north", "amount": 15}, {"region": "south", "amount": 99}]})
            structured = rag("POST", "/query", json={"question": "north sales", "mode": "structured",
                "row_filters": [{"field": "region", "op": "eq", "value": "north"}], "aggregate": {"operation": "sum", "field": "amount"}})
            assert structured["structured_result"]["aggregate"]["value"] == 25
            checks.append("HTTP historical revisions/purge and typed aggregation")

            run = agent("POST", "/workflows", expected=202, json={"goal": "What is the session timeout?", "idempotency_key": "tcp-dag"})
            state = finish(run["id"])
            assert state["status"] == "completed" and "30 minutes" in state["steps"][-1]["result"]["answer"]
            assert agent("POST", "/workflows", expected=202, json={"goal": "What is the session timeout?", "idempotency_key": "tcp-dag"})["duplicate"]
            request("GET", agent_url, "/workflows/" + run["id"], token="e2e-agent-b", expected=404)
            agent("POST", "/registry/agents", expected=201, json={"name": "calculator", "version": 1,
                "allowed_tools": ["math.calculate"], "evaluator": "arithmetic"})
            loop = agent("POST", "/workflows", expected=202, json={"goal": "Calculate 6 * 7", "runtime_mode": "agent_loop", "agent_id": "calculator"})
            result = finish(loop["id"])
            assert result["status"] == "completed" and result["steps"][0]["result"]["answer"] == "42"
            assert result["steps"][0]["checkpoint"]["finished"]
            no_answer = agent("POST", "/workflows", expected=202, json={"goal": "Explain zeppelin dragon concerto", "runtime_mode": "agent_loop", "max_rounds": 2})
            failed = finish(no_answer["id"])
            assert failed["status"] == "failed" and failed["steps"][0]["result"]["stop_reason"] == "rounds_exhausted"
            agent("PUT", "/memory/notes/lesson", json={"content": "Session timeout is 30 minutes", "ttl_seconds": 120, "source_run_id": run["id"]})
            assert agent("GET", "/memory/notes?q=session")["records"][0]["source_run_id"] == run["id"]
            checks.append("Independent worker DAG, idempotency, registry, bounded loop, explicit failure and memory provenance")

            publication = agent("POST", "/workflows", expected=202, json={"goal": "Publish verified arithmetic", "steps": [
                {"id": "calc", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
                {"id": "publish", "tool": "artifact.publish", "depends_on": ["calc"]}]})
            awaiting = finish(publication["id"])
            assert awaiting["status"] == "waiting_approval"
            path = "/workflows/" + publication["id"] + "/approve/publish"
            agent("POST", path, expected=403, json={"approved": True})
            request("POST", agent_url, path, token="e2e-approver-a", json={"approved": True, "reason": "Verified synthetic arithmetic"})
            published = finish(publication["id"], statuses=("completed", "failed"))
            assert published["status"] == "completed"
            artifact_id = published["steps"][-1]["result"]["artifact_ref"]["artifact_id"]
            assert agent("GET", "/artifacts/" + artifact_id)["calc"]["value"] == 42
            stream = client.get(agent_url + "/workflows/" + publication["id"] + "/events/stream",
                                headers={"Authorization": "Bearer e2e-agent-a"})
            assert stream.status_code == 200 and "event: done" in stream.text
            checks.append("Separated approval credential, persisted artifact, authenticated SSE")

            before_runs = {item["id"] for item in agent("GET", "/workflows")}
            for invalid_body in (b'{"goal":"Calculate 6 * 7","max_cost_usd":1e309}',
                                 b'{"goal":"\\ud800xx"}'):
                rejected = client.post(agent_url + "/workflows", content=invalid_body, headers={
                    "Authorization": "Bearer e2e-agent-a", "Content-Type": "application/json",
                })
                assert rejected.status_code == 422, rejected.text
                assert all(set(item) == {"type", "loc", "msg"} for item in rejected.json()["detail"])
            memory_before = agent("GET", "/memory/notes/lesson")
            agent("PUT", "/memory/notes/lesson", expected=422, content=json.dumps({
                "content": "Do not replace retained memory", "metadata": {"nested": {"label": "\ud800"}},
            }).encode("utf-8"))
            assert agent("GET", "/memory/notes/lesson") == memory_before
            source_before = rag("GET", "/documents/session")
            rag("POST", "/documents", expected=422, content=json.dumps({
                "document_id": "session", "title": "Do not replace retained source", "text": "Rejected content",
                "metadata": {"nested": {"label": "\ud800"}},
            }).encode("utf-8"))
            assert rag("GET", "/documents/session") == source_before
            checks.append("Real HTTP rejects unsafe JSON before workflow, memory or source mutation")

            body = json.dumps({"goal": "Calculate 2 + 3", "idempotency_key": "tcp-hook"}).encode()
            stamp = str(int(time.time()))
            digest = hmac.new(hook_secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            headers = {"X-Agent-Hook": "e2e", "X-Agent-Timestamp": stamp, "X-Agent-Signature": "sha256=" + digest, "Content-Type": "application/json"}
            for field, value in ((b"X-Agent-Timestamp", b"\xb2"), (b"X-Agent-Signature", b"sha256=\xe9")):
                raw_headers = {key.encode("ascii"): value.encode("ascii") for key, value in headers.items()}
                raw_headers[field] = value
                rejected = client.post(agent_url + "/hooks/workflows", content=body, headers=raw_headers)
                assert rejected.status_code == 401, rejected.text
            assert {item["id"] for item in agent("GET", "/workflows")} == before_runs
            hook = client.post(agent_url + "/hooks/workflows", content=body, headers=headers)
            assert hook.status_code == 202 and finish(hook.json()["id"])["status"] == "completed"
            assert client.post(agent_url + "/hooks/workflows", content=body, headers=headers).status_code == 409
            checks.append("Real signed webhook, malformed-header rejection without execution and replay prevention")
            subprocess.run([sys.executable, str(ROOT / "tests" / "smoke_mcp.py")], env=env, cwd=ROOT, check=True, timeout=45)
            checks.append("Both real stdio MCP handshakes and HTTP-backed tool calls")
            print(json.dumps({"status": "passed", "checks": checks, "storage": storage,
                              "live_paid_provider": False}, indent=2))
        except BaseException:
            for name, process, logfile in processes:
                logfile.flush()
                logfile.seek(0)
                print(name, "exit=", process.poll(), logfile.read()[-4000:], file=sys.stderr)
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
