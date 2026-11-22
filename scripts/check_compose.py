"""Verify rendered configuration and the real PostgreSQL Compose deployment.

Run bootstrap_compose.py first. No credentials or full rendered configuration
are written to reports. --live requires the actual running Docker services.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
FULL_STACK = (ROOT / "agentic").is_dir()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def compose(*args, env=None):
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=120)
    require(result.returncode == 0, "Docker Compose command failed: " + " ".join(args[:2]))
    return result.stdout


def read_clients():
    result = {}
    for line in (ROOT / "data/client.env").read_text().splitlines():
        key, _, raw = line.partition("=")
        result[key] = shlex.split(raw)[0]
    return result


def configured():
    baseline = json.loads(compose("config", "--format", "json"))
    services = baseline["services"]
    require(set(services) == ({"postgres", "rags", "api", "worker"} if FULL_STACK else {"postgres", "rags"}),
            "Unexpected default deployment services")
    require(not services["postgres"].get("ports"), "Database must not expose a host port")
    require(services["postgres"]["image"] == "pgvector/pgvector:0.8.7-pg17", "Unexpected database image")
    rag = services["rags"]["environment"]
    require(rag["RAGS_MODEL_PROVIDER"] == "extractive" and rag["RAGS_EMBEDDING_PROVIDER"] == "local",
            "Default deployment must remain provider-credential-free")
    require(rag["RAGS_DATABASE_URL"].startswith("postgresql://rags_app:"), "RAG is not using its application role")
    admin_password = services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    require(all(admin_password not in str(value) for name in services if name != "postgres"
                for value in services[name].get("environment", {}).values()), "Admin password reached an application")
    for name in services:
        for port in services[name].get("ports", []):
            require(port.get("host_ip") == "127.0.0.1", "HTTP port is not localhost-only")
    if FULL_STACK:
        agent = services["api"]["environment"]
        require(agent == services["worker"]["environment"], "API and worker configurations diverged")
        require(agent["AGENT_MODEL_PROVIDER"] == "offline", "Default Agent provider is not offline")
        require(agent["AGENT_DATABASE_URL"].startswith("postgresql://agent_app:"), "Agent is not using its application role")
    overrides = {
        "RAGS_EMBEDDING_PROVIDER": "openai", "RAGS_MODEL_PROVIDER": "openai",
        "RAGS_EMBEDDING_BASE_URL": "https://embedding.example/v1",
        "RAGS_EMBEDDING_API_KEY": "fixture-embedding-key", "RAGS_EMBEDDING_MODEL": "fixture-embedding",
        "RAGS_GENERATION_BASE_URL": "https://generation.example/v1",
        "RAGS_GENERATION_API_KEY": "fixture-generation-key", "RAGS_GENERATION_MODEL": "fixture-generation",
        "RAGS_FEATURES_JSON": '{"deletion":false}', "RAGS_CONTEXT_BUDGET": "4096",
        "RAGS_CHUNKING_JSON": '{"strategy":"recursive","max_tokens":256,"overlap_tokens":32}',
    }
    if FULL_STACK:
        overrides.update({
            "AGENT_FEATURES_JSON": '{"math":false}', "AGENT_RAG_TIMEOUT_SECONDS": "90",
            "AGENT_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "fixture-agent-key",
            "AGENT_MODEL_ROUTES_JSON": '[{"name":"fixture","provider":"anthropic","model":"fixture","api_key_env":"ANTHROPIC_API_KEY"}]',
            "AGENT_TENANT_POLICIES_JSON": '{"local":{"allowed_namespaces":["default","notes"]}}',
        })
    rendered = json.loads(compose("config", "--format", "json", env={**os.environ, **overrides}))["services"]
    for key, value in overrides.items():
        names = ("rags",) if key.startswith("RAGS_") else ("api", "worker")
        for name in names:
            require(rendered[name]["environment"].get(key) == value, "Compose did not forward " + key + " to " + name)
    return {"id": "configuration_and_role_wiring", "passed": True, "application_environments_equal": FULL_STACK}


def request(base, path, token=None, data=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(base + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def ready(base):
    deadline = time.monotonic() + 45
    while True:
        try:
            if request(base, "/live")["status"] == "ok":
                return
        except (OSError, urllib.error.URLError):
            pass
        require(time.monotonic() < deadline, "HTTP service did not become ready")
        time.sleep(0.2)


def live():
    clients = read_clients()
    rag_url, rag_key = clients["RAGS_URL"], clients["RAGS_API_KEY"]
    ready(rag_url)
    health = request(rag_url, "/health", rag_key)
    require(health["storage"] == {"backend": "postgresql", "vector_search": "pgvector_exact"},
            "Compose RAG did not use actual PostgreSQL/pgvector")
    document_id = "compose-" + uuid.uuid4().hex
    source = request(rag_url, "/documents", rag_key, {
        "document_id": document_id, "title": "Docker verification", "metadata": {"compose": document_id},
        "text": "Session expires after 45 minutes of inactivity.",
    })
    query = {"question": "When does the session expire?", "mode": "dense",
             "filters": {"metadata": {"compose": document_id}}}
    result = request(rag_url, "/query", rag_key, query)
    require(result["citations"] and "45 minutes" in result["answer"], "Actual vector-backed query failed")
    vector_type = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "rags", "-Atc",
                          "SELECT pg_typeof(embedding)::text FROM chunks LIMIT 1;").strip()
    require(vector_type == "vector", "Dense embeddings are not native PostgreSQL vectors")
    version = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "rags", "-Atc",
                      "SELECT extversion FROM pg_extension WHERE extname='vector';").strip()
    require(version == "0.8.7", "Expected pgvector extension is not active")
    role = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "postgres", "-Atc",
                   "SELECT rolsuper FROM pg_roles WHERE rolname='rags_app';").strip()
    require(role == "f", "RAG application role is a superuser")
    run_id = None
    if FULL_STACK:
        agent_url, key = clients["AGENT_URL"], clients["AGENT_API_KEY"]
        ready(agent_url)
        capabilities = request(agent_url, "/capabilities", key)
        require(capabilities["storage"]["backend"] == "postgresql", "Compose Agent is not using PostgreSQL")
        run_id = request(agent_url, "/workflows", key, {"goal": "When does the session expire?"})["id"]
        deadline = time.monotonic() + 45
        while True:
            state = request(agent_url, "/workflows/" + run_id, key)
            if state["status"] in {"completed", "failed"}:
                break
            require(time.monotonic() < deadline, "Independent worker did not finish")
            time.sleep(0.2)
        require(state["status"] == "completed" and state["steps"][0]["result"]["citations"]
                and "45 minutes" in state["steps"][-1]["result"]["answer"], "Full PostgreSQL worker flow failed")
        isolation = compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "postgres", "-Atc",
                            "SELECT rolsuper, has_database_privilege('rags_app','agentic','CONNECT'), "
                            "has_database_privilege('agent_app','rags','CONNECT') "
                            "FROM pg_roles WHERE rolname='agent_app';").strip()
        require(isolation == "f|f|f", "Application database roles are not isolated")
    compose("restart", *("rags", "api", "worker") if FULL_STACK else ("rags",))
    ready(rag_url)
    require(request(rag_url, "/documents/" + document_id, rag_key)["revision"] == source["revision"],
            "Source revision did not survive process restart")
    require("45 minutes" in request(rag_url, "/query", rag_key, query)["answer"], "Vector query did not survive restart")
    if FULL_STACK:
        ready(agent_url)
        require(request(agent_url, "/workflows/" + run_id, key)["status"] == "completed",
                "Durable workflow did not survive process restart")
    return {"id": "actual_pgvector_stack_and_restart", "passed": True, "native_vector_type": vector_type,
            "vector_extension": version, "independent_worker": FULL_STACK, "data_survived_restart": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = {"status": "failed", "checks": [], "live_paid_provider": False}
    try:
        report["checks"].append(configured())
        if args.live:
            report["checks"].append(live())
        report["status"] = "passed"
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        raise
    finally:
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        report["measured_at"] = datetime.now(timezone.utc).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "completed_checks": len(report["checks"])}))


if __name__ == "__main__":
    main()
