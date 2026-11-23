"""Actual pinned LiteLLM + PostgreSQL + verified TLS acceptance, using synthetic models.

Run with the Agentic development environment and a separately installed LiteLLM
1.110.2 interpreter. AGENT_TEST_POSTGRES_URL must select a disposable test server
whose role can create databases. The harness creates and drops only its new UUID
database. It never loads model weights or calls a hosted model.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
ALIAS = "gateway/inkling-2-mini-iq4nl-v1"
UPSTREAM_MODEL = "thinkingmachines/Inkling-2-Mini"
PRIVATE_REASONING = "fixture-private-reasoning-should-not-be-public-8f43"
PROMPT_MARKER = "fixture-private-prompt-should-not-be-logged-6a31"


class AcceptanceFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


@contextmanager
def environment(values):
    previous = dict(os.environ)
    try:
        for key in tuple(os.environ):
            if key.startswith(("RAGS_", "OPENAI_", "ANTHROPIC_")):
                os.environ.pop(key)
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


@contextmanager
def temporary_gateway_database(admin_url):
    parsed = urlsplit(admin_url)
    require(parsed.scheme in {"postgresql", "postgres"} and parsed.hostname is not None,
            "invalid_test_database_url")
    # UUID is generated here, never read from CLI or an existing application DB.
    name = "litellm_acceptance_" + uuid.uuid4().hex
    gateway_url = urlunsplit(parsed._replace(path="/" + name, query="", fragment=""))
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield gateway_url
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class Fixture:
    def __init__(self):
        self.counts = Counter()
        self.lock = Lock()
        self.release = Event()
        self.key = ""
        self.failed_auth = 0

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    self.send_error(413)
                    return
                payload = json.loads(self.rfile.read(length))
                rendered = json.dumps(payload.get("messages", []))
                found = re.search(r"FIXTURE_CASE:([a-z_]+)", rendered)
                case = found.group(1) if found else "normal"
                with fixture.lock:
                    fixture.counts[case] += 1
                    authorized = self.headers.get("Authorization") == "Bearer " + fixture.key
                    fixture.failed_auth += int(not authorized)
                if not authorized:
                    self.send_error(401)
                    return
                if payload.get("model") != UPSTREAM_MODEL:
                    self.send_error(400)
                    return
                if case == "transient":
                    self.reply(503, {"error": {"message": "Synthetic transient failure",
                                              "type": "server_error", "code": "fixture_503"}})
                    return
                if case == "slow":
                    fixture.release.wait(2)
                message = {"role": "assistant", "content": "Fixture answer"}
                finish = "stop"
                if case in {"tool", "truncated_tool"}:
                    message["content"] = None
                    message["tool_calls"] = [{
                        "id": "call_fixture", "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query":"synthetic"}'},
                    }]
                    finish = "tool_calls" if case == "tool" else "length"
                if case == "reasoning":
                    message["reasoning_content"] = PRIVATE_REASONING
                if case == "truncated":
                    finish = "length"
                self.reply(200, {
                    "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
                    "created": 1700000000, "model": UPSTREAM_MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                })

            def reply(self, status, payload):
                body = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        return Handler

    @contextmanager
    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(3)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def check(args):
    report = {
        "schema_version": "litellm-process-acceptance-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running", "evidence_kind": "actual_gateway_synthetic_upstream",
        "model_inference_executed": False, "gateway_version": None, "checks": [],
        "gateway_store_backend": "postgresql", "application_store_backend": "sqlite",
        "full_two_service_http_worker_exercised": False,
        "stage": "preflight",
    }
    secrets = []
    diagnostic = ""
    process = None
    log_path = None

    def passed(name, **details):
        report["checks"].append({"name": name, "status": "passed", **details})

    try:
        # Preserve the venv executable path: resolving its symlink selects the
        # base interpreter and loses the separately pinned gateway environment.
        gateway_python = args.gateway_python.absolute()
        require(gateway_python.is_file(), "gateway_interpreter_missing")
        version = subprocess.run(
            [str(gateway_python), "-c", 'from importlib.metadata import version; print(version("litellm"))'],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        report["gateway_version"] = version
        require(version == "1.110.2", "gateway_version_does_not_match_pin")
        runtime = subprocess.run(
            [str(gateway_python), "-c", "from importlib.metadata import version; import json,platform; "
             "print(json.dumps({'python': platform.python_version(), 'packages': "
             "{name:version(name) for name in ('litellm','prisma','httpx','fastapi')}}))"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        report["gateway_runtime"] = json.loads(runtime.stdout)
        client_ready = subprocess.run(
            [str(gateway_python), "-c", "from importlib.metadata import distribution; "
             "from pathlib import Path; import sys; "
             "sys.exit(0 if Path(distribution('prisma').locate_file('prisma/client.py')).is_file() else 1)"],
            capture_output=True, text=True, timeout=10,
        )
        require(client_ready.returncode == 0, "gateway_prisma_client_not_generated")
        require((args.rags_path / "rags/agents/router.py").is_file(), "rags_checkout_missing")
        source_files = [ROOT / "deploy/litellm/proxy.yaml", ROOT / "deploy/litellm/init_keys.py",
                        ROOT / "scripts/bootstrap_litellm.py", ROOT / "scripts/check_litellm.py"]
        source_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in source_files}
        report["source_sha256"] = source_hashes
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(args.rags_path.resolve()))
        admin_url = os.environ.get("AGENT_TEST_POSTGRES_URL", "")
        require(bool(admin_url), "disposable_postgres_url_required")
        secrets.append(admin_url)
        bootstrap = load_module("litellm_acceptance_bootstrap", ROOT / "scripts/bootstrap_litellm.py")
        initializer = load_module("litellm_acceptance_keys", ROOT / "deploy/litellm/init_keys.py")
        fixture = Fixture()
        with tempfile.TemporaryDirectory(prefix="litellm-acceptance-") as temporary:
            directory = Path(temporary)
            log_path = directory / "gateway.log"
            try:
                with temporary_gateway_database(admin_url) as database_url, fixture.serve() as upstream:
                    with psycopg.connect(database_url, connect_timeout=5) as database:
                        report["gateway_database_version"] = database.execute(
                            "SELECT current_setting('server_version')").fetchone()[0]
                    private = directory / "gateway"
                    bootstrap.create(private, upstream)
                    proxy_env = read_env(private / "proxy.env")
                    proxy_env["DATABASE_URL"] = database_url
                    fixture.key = proxy_env["INKLING_UPSTREAM_API_KEY"]
                    master = proxy_env["LITELLM_MASTER_KEY"]
                    rags_key = (private / "rags/api.key").read_text().strip()
                    agent_key = (private / "agentic/api.key").read_text().strip()
                    secrets.extend([database_url, master, rags_key, agent_key, fixture.key,
                                    proxy_env["LITELLM_SALT_KEY"]])
                    require(rags_key != agent_key != master and rags_key != master, "service_keys_not_distinct")
                    port = free_port()
                    origin = f"https://127.0.0.1:{port}"
                    # Only runtime/cache locations cross the process boundary;
                    # unrelated cloud/model credentials and ambient proxies do not.
                    inherited = {
                        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "TIKTOKEN_CACHE_DIR",
                        "PRISMA_QUERY_ENGINE_BINARY", "PRISMA_SCHEMA_ENGINE_BINARY",
                    }
                    child_env = {name: value for name, value in os.environ.items() if name in inherited}
                    child_env.update(proxy_env)
                    child_env.update({
                        "PATH": str(gateway_python.parent) + os.pathsep + child_env.get("PATH", ""),
                        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_LOG": "ERROR",
                        "NO_PROXY": "127.0.0.1,localhost", "DO_NOT_TRACK": "1",
                    })
                    report["stage"] = "gateway_startup"
                    with log_path.open("wb") as output:
                        process = subprocess.Popen([
                            str(gateway_python), "-c", "from litellm_cli.main import proxy_server; proxy_server()",
                            "--config", str(private / "proxy.yaml"), "--host", "127.0.0.1", "--port", str(port),
                            "--ssl_keyfile_path", str(private / "server.key"),
                            "--ssl_certfile_path", str(private / "server.pem"),
                        ], env=child_env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    context = ssl.create_default_context(cafile=str(private / "ca.pem"))
                    with httpx.Client(base_url=origin, verify=context, trust_env=False, timeout=5) as client:
                        deadline = time.monotonic() + 100
                        while True:
                            require(process.poll() is None, "gateway_exited_during_startup")
                            try:
                                if client.get("/health/liveliness").status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            require(time.monotonic() < deadline, "gateway_startup_timeout")
                            time.sleep(0.1)
                        passed("actual_pinned_proxy_with_postgres_and_verified_tls")
                        report["stage"] = "key_provisioning"
                        initializer.provision(private, origin, master)
                        initializer.provision(private, origin, master)
                        passed("idempotent_distinct_service_key_provisioning")
                        for service, key in (("rags", rags_key), ("agentic", agent_key)):
                            headers = {"Authorization": "Bearer " + key}
                            models = client.get("/v1/models", headers=headers)
                            require(models.status_code == 200, "service_model_listing_failed")
                            require({item["id"] for item in models.json()["data"]} == {ALIAS},
                                    "service_model_allowlist_incorrect")
                            management = client.post("/key/generate", headers=headers, json={"models": [ALIAS]})
                            require(management.status_code in {401, 403}, "service_key_can_manage_keys")
                            denied = client.post("/v1/chat/completions", headers=headers, json={
                                "model": "openai/unapproved", "messages": [{"role": "user", "content": "deny"}]})
                            require(denied.status_code in {400, 401, 403}, "service_key_can_choose_unapproved_model")
                            embeddings = client.post("/v1/embeddings", headers=headers, json={"model": ALIAS, "input": "deny"})
                            require(embeddings.status_code in {401, 403}, "service_key_can_access_embeddings")
                            passed(service + "_key_model_and_endpoint_isolation")
                        wrong = client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-invalid-fixture"}, json={
                            "model": ALIAS, "messages": [{"role": "user", "content": "deny"}]})
                        require(wrong.status_code in {401, 403}, "unknown_gateway_key_accepted")
                        require(sum(fixture.counts.values()) == 0, "denied_request_reached_upstream")
                        passed("denied_credentials_and_routes_make_zero_upstream_calls")
                    report["stage"] = "application_protocols"
                    from rags.agents.config import AgentPlatformConfig
                    from rags.agents.engine import AgentEngine, ToolRegistry
                    from rags.agents.memory import scoped_session
                    from rags.agents.router import ModelRouter
                    from rags.agents.state import StateStore
                    from rags.auth import PERMISSIONS, Principal
                    from rags.config import Settings as RagSettings
                    from rags.features import FeatureFlags
                    from rags.providers import Providers
                    from rags.registry import CapabilityError
                    from rags.storage import Store as RagStore
                    router_config = AgentPlatformConfig(
                        gateway_url=origin + "/v1", gateway_key=rags_key,
                        gateway_ca_file=str(private / "rags/ca.pem"), models={"main": ALIAS},
                        model_options={"main": {"protocol": "openai"}}, max_output_tokens=128)
                    router = ModelRouter(router_config)
                    plain = router.complete(model="main", messages=[{"role": "user", "content": "FIXTURE_CASE:normal"}])
                    require(plain.content == "Fixture answer" and plain.usage.get("total_tokens") == 10,
                            "rag_router_generation_or_usage_mismatch")
                    passed("rags_production_router_generation_usage")
                    declarations = [{"type": "function", "function": {"name": "lookup", "parameters": {
                        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                        "additionalProperties": False}}}]
                    tool = router.complete(model="main", messages=[{"role": "user", "content": "FIXTURE_CASE:tool"}],
                                           tools=declarations)
                    require(len(tool.tool_calls) == 1 and tool.tool_calls[0].arguments == {"query": "synthetic"},
                            "rag_tool_json_contract_failed")
                    try:
                        router.complete(model="main", messages=[{"role": "user", "content": "FIXTURE_CASE:truncated_tool"}],
                                        tools=declarations)
                    except CapabilityError as error:
                        require(error.code == "invalid_model_output", "wrong_truncated_tool_error")
                    else:
                        raise AcceptanceFailure("truncated_tool_accepted")
                    passed("rags_tool_json_and_truncated_tool_rejection")
                    rag_state = StateStore(RagStore(str(directory / "rags.db")))
                    engine = AgentEngine(config=router_config, router=router, state=rag_state,
                                         features=FeatureFlags(), registry=ToolRegistry())
                    principal = Principal("fixture", "owner", frozenset(PERMISSIONS), "api_key")
                    completed = engine.start(principal, goal="FIXTURE_CASE:reasoning " + PROMPT_MARKER,
                                             model="main", session_id="gateway")
                    public = json.dumps([completed.public(), engine.summary(completed),
                                         engine.history.last("fixture", scoped_session("owner", "gateway"))])
                    require(completed.status == "completed" and PRIVATE_REASONING not in public,
                            "native_agent_reasoning_disclosure")
                    passed("native_agent_public_state_and_history_exclude_reasoning")
                    with environment({
                        "RAGS_ENV": "test", "RAGS_DB_PATH": str(directory / "providers.db"),
                        "RAGS_ROUTER_GATEWAY_URL": origin + "/v1", "RAGS_ROUTER_API_KEY": rags_key,
                        "RAGS_ROUTER_CA_FILE": str(private / "rags/ca.pem"),
                        "RAGS_ROUTER_MODELS_JSON": json.dumps({"main": ALIAS}),
                        "RAGS_ROUTER_MODEL_OPTIONS_JSON": '{"main":{"protocol":"openai"}}',
                        "RAGS_MODEL_PROVIDER": "router", "RAGS_GENERATION_MODEL": "main",
                        "RAGS_EMBEDDING_PROVIDER": "local",
                    }):
                        provider = Providers(RagSettings.from_env())
                        require(provider.complete("Synthetic fixture", "FIXTURE_CASE:normal") == "Fixture answer",
                                "rag_provider_integration_failed")
                        try:
                            provider.complete("Synthetic fixture", "FIXTURE_CASE:truncated")
                        except CapabilityError as error:
                            require(error.code == "invalid_model_output", "wrong_rag_truncation_error")
                        else:
                            raise AcceptanceFailure("rag_provider_accepted_truncation")
                    passed("rags_production_providers_generation_and_truncation")
                    from agentic.config import ModelRoute, Settings
                    from agentic.model import ModelGateway
                    from agentic.schemas import Step, WorkflowIn
                    from agentic.store import Store
                    route = ModelRoute(
                        "gateway-fixture-v1", "openai", ALIAS, origin + "/v1", agent_key,
                        output_token_parameter="max_tokens", ca_file=str(private / "agentic/ca.pem"))
                    config = Settings(
                        db_path=str(directory / "agentic.db"), keys={"fixture": "fixture"},
                        rag_url="http://127.0.0.1:1", rag_keys={"fixture": "fixture"}, provider="routed",
                        openai_key="", openai_base_url="https://unused.invalid", openai_model="unused",
                        anthropic_key="", anthropic_model="unused", max_parallel=1, routes=(route,),
                        max_model_output_tokens=128, max_model_tokens=12000)
                    store = Store(config.db_path)
                    run_id = store.submit("fixture", WorkflowIn(goal="Synthetic gateway test", steps=[
                        Step(id="calculate", tool="math.calculate", args={"expression": "2+3"})
                    ]))["id"]
                    model = ModelGateway(config, store)
                    answer = asyncio.run(model.complete("Synthetic fixture", "FIXTURE_CASE:reasoning " + PROMPT_MARKER,
                                                        tenant="fixture", run_id=run_id))
                    usage = store.usage("fixture", run_id)
                    require(answer == "Fixture answer" and PRIVATE_REASONING not in json.dumps(usage),
                            "agentic_model_reasoning_disclosure")
                    require(usage["charged_tokens"] == 10 and usage["usage_complete"] and not usage["cost_complete"],
                            "agentic_reported_usage_or_unknown_cost_mismatch")
                    passed("agentic_real_model_gateway_reported_usage_unknown_cost")
                    try:
                        asyncio.run(model.complete("Synthetic fixture", "FIXTURE_CASE:truncated",
                                                   tenant="fixture", run_id=run_id))
                    except ValueError as error:
                        require("truncated" in str(error), "wrong_agentic_truncation_error")
                    else:
                        raise AcceptanceFailure("agentic_accepted_truncation")
                    require(store.usage("fixture", run_id)["charged_tokens"] == 20,
                            "truncated_response_usage_discarded")
                    passed("agentic_truncation_keeps_provider_usage")
                    report["stage"] = "deadline_and_retry_contracts"
                    for application in ("rags", "agentic"):
                        started = time.monotonic()
                        try:
                            if application == "rags":
                                router.complete(model="main", messages=[{"role": "user", "content": "FIXTURE_CASE:slow"}],
                                                timeout_seconds=0.1)
                            else:
                                limited = ModelGateway(replace(config, model_timeout_seconds=0.1), store)
                                asyncio.run(limited.complete("Synthetic fixture", "FIXTURE_CASE:slow",
                                                             tenant="fixture", run_id=run_id))
                        except (CapabilityError, TimeoutError) as error:
                            if isinstance(error, CapabilityError):
                                require(error.code == "model_timeout", "wrong_rag_deadline_error")
                        else:
                            raise AcceptanceFailure(application + "_deadline_not_enforced")
                        elapsed = time.monotonic() - started
                        require(elapsed < 0.75, application + "_deadline_returned_too_late")
                        passed(application + "_complete_100ms_deadline", elapsed_seconds=round(elapsed, 4))
                    fixture.release.set()
                    time.sleep(0.3)
                    before = fixture.counts["transient"]
                    try:
                        router.complete(model="main", messages=[{"role": "user", "content": "FIXTURE_CASE:transient"}])
                    except CapabilityError as error:
                        require(error.code == "model_gateway_failed", "wrong_transient_error")
                    else:
                        raise AcceptanceFailure("transient_failure_became_success")
                    require(fixture.counts["transient"] - before == 1, "gateway_retried_upstream_request")
                    passed("single_upstream_attempt_without_retry_or_fallback")
                    require(fixture.failed_auth == 0, "upstream_authentication_failed")
                    accounting = store.usage("fixture", run_id)
                    require(any(row["status"] == "uncertain_timeout" for row in accounting["calls"]),
                            "timed_out_agentic_request_lost_conservative_charge")
                    require(PRIVATE_REASONING not in json.dumps(accounting), "reasoning_in_usage_ledger")
                    passed("timeout_reservation_is_conservatively_settled")
                    report["upstream_request_counts"] = dict(sorted(fixture.counts.items()))
                    report["agentic_usage"] = {
                        "calls": len(accounting["calls"]), "charged_tokens": accounting["charged_tokens"],
                        "usage_complete": accounting["usage_complete"], "cost_complete": accounting["cost_complete"],
                        "statuses": [row["status"] for row in accounting["calls"]],
                    }
                    report["stage"] = "deleted_key_restart_refusal"
                    with httpx.Client(base_url=origin, verify=context, trust_env=False, timeout=5) as client:
                        administrative = {"Authorization": "Bearer " + master}
                        removed = client.post("/key/delete", headers=administrative, json={"keys": [rags_key]})
                        require(removed.status_code == 200, "service_key_delete_failed")
                        try:
                            initializer.provision(private, origin, master)
                        except initializer.ProvisionError:
                            pass
                        else:
                            raise AcceptanceFailure("deleted_service_key_was_recreated_or_accepted")
                        denied = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + rags_key},
                                             json={"model": ALIAS, "messages": [{"role": "user", "content": "deny"}]})
                        require(denied.status_code in {401, 403}, "deleted_service_key_was_resurrected")
                    passed("deleted_service_key_is_never_resurrected_by_provisioning")
                    report["stage"] = "log_privacy_and_cleanup"
                    stop_process(process)
                    logs = log_path.read_text(errors="replace")
                    require(PRIVATE_REASONING not in logs and PROMPT_MARKER not in logs,
                            "private_content_in_gateway_process_log")
                    passed("gateway_process_logs_exclude_fixture_prompt_and_reasoning")
                require(all(hashlib.sha256(path.read_bytes()).hexdigest() == source_hashes[str(path.relative_to(ROOT))]
                            for path in source_files), "source_changed_during_acceptance")
                report["status"] = "passed"
                report["stage"] = "completed"
            finally:
                stop_process(process)
                if log_path.exists():
                    diagnostic = log_path.read_text(errors="replace")[-16000:]
    except Exception as error:
        report["status"] = "failed"
        report["failure_code"] = str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__
        if args.diagnostics:
            for secret in sorted(secrets, key=len, reverse=True):
                if secret:
                    diagnostic = diagnostic.replace(secret, "[fixture-credential]")
            diagnostic = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[fixture-credential]", diagnostic)
            print(diagnostic, file=sys.stderr)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-python", type=Path, required=True)
    parser.add_argument("--rags-path", type=Path, default=ROOT.parent / "RAGs")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/litellm-process-acceptance.json")
    parser.add_argument("--diagnostics", action="store_true", help="Print redacted synthetic gateway startup logs on failure")
    args = parser.parse_args()
    report = check(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    pending = args.report.with_suffix(args.report.suffix + ".tmp")
    pending.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    pending.replace(args.report)
    print(json.dumps({"status": report["status"], "stage": report["stage"],
                      "passed_checks": len(report["checks"]), "failure_code": report.get("failure_code"),
                      "model_inference_executed": False}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
