"""Render and validate the optional gateway stack with the actual Compose CLI.

Disposable bootstrap files remain inside a private temporary directory. No
rendered environment, credential or private key is printed or retained in the
report. This checks configuration, not container execution or model inference.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FILES = ("compose.yaml", "compose.litellm.yaml", "deploy/litellm/compose.yaml",
         "deploy/litellm/proxy.yaml", "deploy/litellm/init_keys.py",
         "scripts/bootstrap_compose.py", "scripts/bootstrap_litellm.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_script(name):
    spec = importlib.util.spec_from_file_location("compose_verification_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(compose: list[str]) -> dict:
    source = {name: (ROOT / name).read_bytes() for name in FILES}
    version = subprocess.check_output([*compose, "version", "--short"], text=True, timeout=10).strip()
    with tempfile.TemporaryDirectory(prefix="agentic-gateway-compose-") as temporary:
        work = Path(temporary) / "agentic-system"
        work.mkdir()
        for name, content in source.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        load_script("bootstrap_compose").create(work)
        load_script("bootstrap_litellm").create(work / "data/litellm")
        env = {key: value for key, value in os.environ.items() if not key.startswith((
            "AGENT_", "RAGS_", "OPENAI_", "ANTHROPIC_", "TINKER_", "LITELLM_", "INKLING_", "POSTGRES_", "AWS_", "OTEL_",
        ))}
        result = subprocess.run([
            *compose, "--project-directory", str(work), "--env-file", str(work / ".env"),
            "-f", str(work / "compose.yaml"), "-f", str(work / "compose.litellm.yaml"),
            "--profile", "litellm", "config", "--format", "json",
        ], env=env, capture_output=True, text=True, check=True, timeout=30)
        services = json.loads(result.stdout)["services"]
        require(set(services) == {"api", "worker", "rags", "postgres", "litellm", "litellm-db", "litellm-init"},
                "Unexpected gateway stack services")
        initializer = services["litellm-init"]
        require(initializer["user"] == "0:0" and initializer["restart"] == "no"
                and initializer["cap_drop"] == ["ALL"] and initializer["cap_add"] == ["DAC_OVERRIDE"]
                and initializer["security_opt"] == ["no-new-privileges:true"],
                "Private host-owned receipts require only DAC_OVERRIDE in the one-shot initializer")
        expected_mounts = {
            "/bootstrap": (work / "data/litellm/receipts", False),
            "/bootstrap/ca.pem": (work / "data/litellm/ca.pem", True),
            "/bootstrap/rags/api.key": (work / "data/litellm/rags/api.key", True),
            "/bootstrap/agentic/api.key": (work / "data/litellm/agentic/api.key", True),
            "/bootstrap-tools/init_keys.py": (work / "deploy/litellm/init_keys.py", True),
        }
        mounts = initializer["volumes"]
        require(len(mounts) == len(expected_mounts)
                and {item["target"] for item in mounts} == set(expected_mounts),
                "Initializer mounts must exclude CA signing, gateway, database and upstream secrets")
        for item in mounts:
            path, read_only = expected_mounts[item["target"]]
            require(item["type"] == "bind" and item["source"] == str(path)
                    and bool(item.get("read_only", False)) == read_only,
                    "Only the initializer receipt directory may be writable")
        require(stat.S_IMODE((work / "data/litellm").stat().st_mode) == 0o700
                and stat.S_IMODE((work / "data/litellm/receipts").stat().st_mode) == 0o700,
                "Bootstrap secrets and receipts must remain host-private")
        require(set(initializer["environment"]) == {"LITELLM_MASTER_KEY"},
                "Initializer must receive only its required management credential")
        for name in ("api", "worker", "rags"):
            service = services[name]
            require(service["depends_on"]["litellm-init"]["condition"] == "service_completed_successfully",
                    "Applications require completed key provisioning")
            require(service["depends_on"]["postgres"]["condition"] == "service_healthy",
                    "Application database readiness must survive override merging")
            require("LITELLM_MASTER_KEY" not in service["environment"], "Application received the master key")
            require(not service.get("cap_add"), "Applications must not inherit initializer capabilities")
            mounts = [item for item in service["volumes"] if item.get("target") == "/app/gateway"]
            owner = "rags" if name == "rags" else "agentic"
            require(len(mounts) == 1 and mounts[0]["read_only"]
                    and mounts[0]["source"] == str(work / "data/litellm" / owner),
                    "Gateway credentials require a service-specific read-only mount")
        rag = services["rags"]["environment"]
        require(rag["RAGS_ENV"] == "production" and rag["RAGS_ROUTER_ALLOW_HTTP"] == "false"
                and rag["RAGS_ROUTER_GATEWAY_URL"] == "https://litellm:4000/v1", "RAG gateway requires production TLS")
        require(rag["RAGS_EMBEDDING_PROVIDER"] == "local", "Gateway override must retain the separate embedding provider")
        require(services["litellm"]["ports"][0]["host_ip"] == "127.0.0.1", "Gateway host port must remain loopback")
        require("ports" not in services["litellm-db"], "Gateway database must not publish a host port")
        from agentic.config import Settings
        for name in ("api", "worker"):
            config = dict(services[name]["environment"])
            routes = json.loads(config["AGENT_MODEL_ROUTES_JSON"])
            for route in routes:
                route["ca_file"] = str(work / "data/litellm/agentic/ca.pem")
            config["AGENT_MODEL_ROUTES_JSON"] = json.dumps(routes)
            config["AGENT_GATEWAY_API_KEY_FILE"] = str(work / "data/litellm/agentic/api.key")
            config["AGENT_FEATURES_FILE"] = str(work / "data/config/agent.features.json")
            original = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(env)
                os.environ.update(config)
                resolved = Settings.from_env()
                require(resolved.routes[0].api_key == (work / "data/litellm/agentic/api.key").read_text().strip(),
                        "Model route must use its designated gateway key")
                require(resolved.routes[0].base_url == "https://litellm:4000/v1"
                        and resolved.routes[0].ca_file == str(work / "data/litellm/agentic/ca.pem"),
                        "Model route must use the gateway and its explicit trust root")
            finally:
                os.environ.clear()
                os.environ.update(original)
    return {"schema_version": 1, "status": "passed", "compose_version": version,
            "services": sorted(services), "gateway_image": services["litellm"]["image"],
            "validations": ["actual_compose_include_merge", "production_https", "service_key_mount_isolation",
                            "master_key_absent_from_applications", "startup_dependency_gate", "database_unpublished",
                            "loopback_gateway_port", "actual_agentic_settings_api_and_worker",
                            "private_host_owned_receipt_access", "initializer_minimal_capabilities_and_secret_mounts"],
            "source_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in source.items()},
            "container_execution": False, "model_inference": False, "credentials_redacted": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-binary", help="Optional standalone Docker Compose binary; defaults to docker compose")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    command = [args.compose_binary] if args.compose_binary else ["docker", "compose"]
    if shutil.which(command[0]) is None:
        raise SystemExit("Docker Compose is required; configuration was not verified")
    try:
        report = check(command)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report = {"schema_version": 1, "status": "failed", "error_type": type(exc).__name__,
                  "container_execution": False, "model_inference": False, "credentials_redacted": True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "container_execution": False, "credentials_redacted": True}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
