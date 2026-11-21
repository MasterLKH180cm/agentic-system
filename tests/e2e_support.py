"""Disposable real services and a fixed-localhost fault proxy for E2E gates.

There are no patched production tools, direct database edits, or inherited model
credentials. Missing dependencies and failed child processes are hard failures.
"""
import asyncio
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx


ROOT = Path(__file__).resolve().parents[1]
QUESTION = "What is the session timeout?"
SOURCE = "Session timeout is 30 minutes after inactivity. Refresh tokens rotate after login."


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def eventually(check, *, timeout=15, description="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("Deadline waiting for " + description)
        time.sleep(0.05)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_environment():
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "VIRTUAL_ENV",
               "LD_LIBRARY_PATH", "SSL_CERT_FILE", "COVERAGE_PROCESS_START", "COVERAGE_FILE"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(PYTHONUNBUFFERED="1", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               HF_HUB_DISABLE_TELEMETRY="1", ORT_DISABLE_TELEMETRY="1")
    return env


@dataclass
class Fault:
    failures: int = 0
    hold_requests: int = 0
    calls: int = 0
    forwarded: int = 0
    finished: int = 0
    release: threading.Event = field(default_factory=threading.Event)


class FaultProxy:
    """Only POST /query may be forwarded, to one constructor-fixed local RAG."""
    def __init__(self, rag_url):
        self.rag_url = rag_url
        self.rules = {}
        self.lock = threading.Lock()
        self.active = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, content):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected when a killed/cancelled worker disconnects.

            def do_POST(self):
                if self.path != "/query":
                    self.reply(404, b'{"detail":"fixed fixture route only"}')
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64_000:
                    self.reply(413, b'{"detail":"fixture body limit"}')
                    return
                body = self.rfile.read(length)
                question = json.loads(body)["question"]
                with owner.lock:
                    rule = owner.rules.get(question)
                    owner.active += 1
                    if rule:
                        rule.calls += 1
                        attempt = rule.calls
                    else:
                        attempt = 0
                try:
                    if rule and attempt <= rule.failures:
                        self.reply(503, b'{"detail":"controlled transient fixture failure"}')
                        return
                    with httpx.Client(timeout=10, trust_env=False) as client:
                        result = client.post(owner.rag_url + "/query", content=body, headers={
                            "Authorization": self.headers.get("Authorization", ""),
                            "Content-Type": "application/json", "Accept-Encoding": "identity",
                        })
                    if rule:
                        with owner.lock:
                            rule.forwarded += 1
                        if attempt <= rule.hold_requests:
                            if not rule.release.wait(timeout=60):
                                self.reply(504, b'{"detail":"fixture hold expired"}')
                                return
                    self.reply(result.status_code, result.content)
                except httpx.HTTPError:
                    self.reply(502, b'{"detail":"fixture upstream unavailable"}')
                finally:
                    with owner.lock:
                        owner.active -= 1
                        if rule:
                            rule.finished += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def fault(self, question, *, failures=0, hold_requests=0):
        rule = Fault(failures=failures, hold_requests=hold_requests)
        with self.lock:
            require(question not in self.rules, "A fixture question must identify one fault scenario")
            self.rules[question] = rule
        return rule

    def close(self):
        for rule in self.rules.values():
            rule.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        eventually(lambda: self.active == 0, timeout=12, description="fault-proxy request cleanup")
        require(not self.thread.is_alive(), "Fault-proxy server thread survived cleanup")


class Services:
    def __init__(self, *, step_timeout=7, postgres=None):
        self.step_timeout, self.postgres = step_timeout, postgres
        self.children = []
        self.diagnostics = {}
        self.proxy = None
        self.cleaned = False

    def start(self, name, command):
        log = self.stack.enter_context(open(self.directory / (name + ".log"), "w+"))
        process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdout=log, stderr=log, start_new_session=True)
        self.children.append((name, process, log))
        return process

    def start_worker(self, name):
        return self.start(name, [sys.executable, "-m", "agentic.worker"])

    def __enter__(self):
        self.stack = ExitStack()
        try:
            self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="agent-complete-e2e-")))
            rag_port, agent_port = free_port(), free_port()
            while agent_port == rag_port:
                agent_port = free_port()
            self.rag_url = f"http://127.0.0.1:{rag_port}"
            self.agent_url = f"http://127.0.0.1:{agent_port}"
            self.tenant = "e2e-" + uuid.uuid4().hex
            self.key = "requester-" + uuid.uuid4().hex
            self.other_key = "other-" + uuid.uuid4().hex
            self.approver = "approver-" + uuid.uuid4().hex
            self.rag_key = "rag-" + uuid.uuid4().hex
            self.other_rag_key = "rag-other-" + uuid.uuid4().hex
            self.env = clean_environment()
            self.env.update({
                "RAGS_ENV": "test", "RAGS_DB_PATH": str(self.directory / "rag.db"),
                "RAGS_KEYS_JSON": json.dumps({self.rag_key: self.tenant, self.other_rag_key: self.tenant + "-other"}),
                "RAGS_MODEL_PROVIDER": "extractive", "RAGS_EMBEDDING_PROVIDER": "local",
                "RAGS_ADMISSION_POLICY": "compatibility", "RAGS_URL": self.rag_url, "RAGS_API_KEY": self.rag_key,
                "AGENT_ENV": "test", "AGENT_DB_PATH": str(self.directory / "agent.db"),
                "AGENT_KEYS_JSON": json.dumps({self.key: self.tenant, self.other_key: self.tenant + "-other"}),
                "AGENT_APPROVER_KEYS_JSON": json.dumps({self.approver: self.tenant}),
                "AGENT_RAG_KEYS_JSON": json.dumps({self.tenant: self.rag_key, self.tenant + "-other": self.other_rag_key}),
                "AGENT_MODEL_PROVIDER": "offline", "AGENT_ARTIFACT_DIR": str(self.directory / "artifacts"),
                "AGENT_RATE_DB_PATH": str(self.directory / "rate.db"), "AGENT_RATE_PER_MINUTE": "5000",
                "AGENT_MAX_PARALLEL": "1", "AGENT_LEASE_SECONDS": "0.6", "AGENT_HEARTBEAT_SECONDS": "0.1",
                "AGENT_STEP_TIMEOUT_SECONDS": str(self.step_timeout), "AGENT_MAX_ATTEMPTS": "2",
                "AGENT_RAG_TIMEOUT_SECONDS": "30", "AGENT_URL": self.agent_url, "AGENT_API_KEY": self.key,
            })
            if self.postgres:
                self.env["AGENT_DATABASE_URL"] = self.postgres
            self.client = self.stack.enter_context(httpx.Client(timeout=15, trust_env=False))
            rag = self.start("rag", [sys.executable, "-m", "uvicorn", "rags.api:app", "--host", "127.0.0.1",
                                     "--port", self.rag_url.rsplit(":", 1)[1]])
            self.ready(self.rag_url, rag)
            self.proxy = FaultProxy(self.rag_url)
            self.env["AGENT_RAG_URL"] = self.proxy.url
            api = self.start("api", [sys.executable, "-m", "uvicorn", "agentic.api:app", "--host", "127.0.0.1",
                                     "--port", self.agent_url.rsplit(":", 1)[1]])
            self.ready(self.agent_url, api)
            self.worker = self.start_worker("worker-1")
            self.request("POST", "/documents", rag=True, expected=201, data={
                "document_id": "session-policy", "title": "Session policy", "text": SOURCE,
            })
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def ready(self, base, process):
        def check():
            require(process.poll() is None, "Service exited before readiness")
            try:
                return self.client.get(base + "/live", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False
        eventually(check, timeout=25, description="HTTP service readiness")

    def request(self, method, path, *, data=None, token=None, expected=200, rag=False):
        base = self.rag_url if rag else self.agent_url
        key = token or (self.rag_key if rag else self.key)
        response = self.client.request(method, base + path, json=data, headers={"Authorization": "Bearer " + key})
        require(response.status_code == expected, f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:500]}")
        return response.json()

    def submit(self, goal, **payload):
        return self.request("POST", "/workflows", data={"goal": goal, **payload}, expected=202)["id"]

    def state(self, run_id, *, token=None):
        return self.request("GET", "/workflows/" + run_id, token=token)

    def finish(self, run_id, *, statuses=("completed", "failed", "cancelled"), timeout=20, token=None):
        def check():
            state = self.state(run_id, token=token)
            return state if state["status"] in statuses else None
        return eventually(check, timeout=timeout, description="terminal workflow " + run_id)

    def events(self, run_id):
        return self.request("GET", f"/workflows/{run_id}/events")

    def __exit__(self, exc_type, exc, traceback):
        errors = []
        try:
            for name, process, log in reversed(self.children):
                try:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=4)
                except Exception as error:
                    errors.append(f"{name}: {type(error).__name__}")
                log.flush()
                log.seek(0)
                if exc_type or errors:
                    self.diagnostics[name] = log.read()[-4000:]
            if self.proxy:
                try:
                    self.proxy.close()
                except Exception as error:
                    errors.append(f"proxy: {type(error).__name__}")
        finally:
            self.stack.close()
        self.cleaned = (not errors and not getattr(self, "directory", Path("/")).exists()
                        and all(process.poll() is not None for _, process, _ in self.children))
        if errors:
            self.diagnostics["cleanup"] = errors
        if not exc_type:
            require(self.cleaned, "Child processes or disposable data survived cleanup")


async def inspect_mcp(services, run_id, status):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentic.mcp_server"], env=services.env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            response = await session.call_tool("get_workflow_status", {"workflow_id": run_id})
            require(not response.isError, "MCP returned an error")
            result = response.structuredContent
            if result is None:
                result = json.loads(next(item.text for item in response.content if item.type == "text"))
            if set(result) == {"result"}:
                result = result["result"]
            require(result["id"] == run_id and result["status"] == status, "MCP disagrees with the durable HTTP result")


def verify_mcp(services, run_id, status):
    asyncio.run(asyncio.wait_for(inspect_mcp(services, run_id, status), timeout=20))


def write_report(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    value["measured_at"] = datetime.now(timezone.utc).isoformat()
    destination.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
