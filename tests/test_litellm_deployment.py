"""Gateway configuration and provisioning contracts; no Docker/model execution.

Certificates are generated and verified by OpenSSL. Provisioning tests exercise
the real bounded management adapter with a synthetic HTTP transport and durable
fixture key registry. All credentials exist only in disposable test directories.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys

import httpx
import pytest
import yaml


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
ALIAS = "gateway/inkling-2-mini-iq4nl-v1"
ROUTES = {"/chat/completions", "/v1/chat/completions", "/models", "/v1/models"}


def load_module(name, relative):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    directory = tmp_path_factory.mktemp("gateway-fixture") / "private"
    module = load_module("litellm_bootstrap_fixture", "scripts/bootstrap_litellm.py")
    names = module.create(directory)
    assert names == sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())
    return directory


@pytest.fixture
def private_config(generated, tmp_path):
    target = tmp_path / "private"
    shutil.copytree(generated, target)
    return target


def test_real_certificate_chain_and_sans_match_gateway_and_loopback_hosts(generated):
    for flag, name in (("-verify_hostname", "litellm"), ("-verify_hostname", "localhost"),
                       ("-verify_ip", "127.0.0.1"), ("-verify_ip", "::1")):
        result = subprocess.run(["openssl", "verify", "-CAfile", str(generated / "ca.pem"),
                                 flag, name, str(generated / "server.pem")],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, "Generated gateway certificate failed chain/SAN verification"
    rejected = subprocess.run(["openssl", "verify", "-CAfile", str(generated / "ca.pem"),
                               "-verify_hostname", "unapproved.invalid", str(generated / "server.pem")],
                              capture_output=True, timeout=10)
    assert rejected.returncode != 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(generated / "server.pem", generated / "server.key")
    client = ssl.create_default_context(cafile=generated / "ca.pem")
    assert client.check_hostname and client.verify_mode == ssl.CERT_REQUIRED
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    server = x509.load_pem_x509_certificate((generated / "server.pem").read_bytes())
    ca = x509.load_pem_x509_certificate((generated / "ca.pem").read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not server.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ExtendedKeyUsageOID.SERVER_AUTH in server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert (server.not_valid_after_utc - server.not_valid_before_utc).days == 90


def test_private_bootstrap_separates_admin_database_upstream_and_service_credentials(generated):
    proxy, database, admin = (read_env(generated / name) for name in ("proxy.env", "db.env", "admin.env"))
    rags_key = (generated / "rags/api.key").read_text().strip()
    agent_key = (generated / "agentic/api.key").read_text().strip()
    secrets = [proxy["LITELLM_MASTER_KEY"], proxy["LITELLM_SALT_KEY"], database["POSTGRES_PASSWORD"],
               proxy["INKLING_UPSTREAM_API_KEY"], rags_key, agent_key]
    assert len(set(secrets)) == 6 and all(len(value) >= 40 for value in secrets)
    assert admin == {"LITELLM_MASTER_KEY": proxy["LITELLM_MASTER_KEY"]}
    assert (generated / "upstream.key").read_text().strip() == proxy["INKLING_UPSTREAM_API_KEY"]
    assert stat.S_IMODE(generated.stat().st_mode) == 0o700
    assert stat.S_IMODE((generated / "receipts").stat().st_mode) == 0o700
    assert list((generated / "receipts").iterdir()) == []
    for name in ("admin.env", "db.env", "proxy.env", "upstream.env", "upstream.key", "ca.key"):
        assert stat.S_IMODE((generated / name).stat().st_mode) == 0o600
    for service, own_key in (("rags", rags_key), ("agentic", agent_key)):
        directory = generated / service
        assert {path.name for path in directory.iterdir()} == {"api.key", "ca.pem"}
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in directory.iterdir())
        assert (directory / "ca.pem").read_bytes() == (generated / "ca.pem").read_bytes()
        for value in set(secrets) - {own_key}:
            assert value not in (directory / "api.key").read_text()
    public_configuration = "\n".join((generated / name).read_text()
                                     for name in ("proxy.yaml", "routes.json", "manifest.json"))
    assert all(value not in public_configuration for value in secrets)
    assert json.loads((generated / "manifest.json").read_text())["inference_executed"] is False


@pytest.mark.parametrize("kind", ["existing_directory", "symlink", "dangling_symlink"])
def test_bootstrap_never_overwrites_or_follows_existing_target(tmp_path, monkeypatch, kind):
    module = load_module("litellm_bootstrap_existing", "scripts/bootstrap_litellm.py")
    target, preserved = tmp_path / "gateway", tmp_path / "preserved"
    if kind == "existing_directory":
        target.mkdir()
        preserved = target / "keep.txt"
        preserved.write_text("synthetic-existing-state")
    else:
        if kind == "symlink":
            preserved.mkdir()
            (preserved / "keep.txt").write_text("synthetic-existing-state")
        target.symlink_to(preserved, target_is_directory=True)
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: pytest.fail("Existing target cannot run OpenSSL"))
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink()}
    with pytest.raises(FileExistsError):
        module.create(target)
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink()}
    assert after == before
    assert target.is_symlink() if kind != "existing_directory" else target.is_dir()


@pytest.mark.parametrize("stage", ["secret_write", "openssl"])
def test_failed_bootstrap_cleans_only_its_new_directory(tmp_path, monkeypatch, stage):
    module = load_module("litellm_bootstrap_cleanup", "scripts/bootstrap_litellm.py")
    preserved = tmp_path / "preserved.txt"
    preserved.write_text("synthetic-existing-state")
    target = tmp_path / "gateway"
    if stage == "secret_write":
        write = module.write

        def fail(path, content, mode=0o600):
            if path.name == "proxy.env":
                path.write_text("synthetic-partial-secret")
                raise OSError("synthetic write failure")
            return write(path, content, mode)

        monkeypatch.setattr(module, "write", fail)
    else:
        monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["openssl"])))
    with pytest.raises((OSError, subprocess.SubprocessError)):
        module.create(target)
    assert not target.exists()
    assert preserved.read_text() == "synthetic-existing-state"


@pytest.mark.parametrize("url", ["ftp://model.invalid", "https://user:secret@model.invalid/v1",
                                "https://model.invalid/v1?key=secret", "https://model.invalid/v1#fragment",
                                "http://model.invalid/v1\nEVIL=synthetic", "http://model.invalid/v1\rEVIL=synthetic"])
def test_bootstrap_rejects_unsafe_upstream_configuration_before_writes(tmp_path, monkeypatch, url):
    module = load_module("litellm_bootstrap_bad_url", "scripts/bootstrap_litellm.py")
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: pytest.fail("Invalid URL cannot run OpenSSL"))
    with pytest.raises(ValueError):
        module.create(tmp_path / "gateway", url)
    assert not (tmp_path / "gateway").exists()


class ReplyStream(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.body


class ManagementRegistry:
    def __init__(self, master, missing_status=404):
        self.master = master
        self.missing_status = missing_status
        self.records = {}
        self.generated = []
        self.calls = []
        self.lookup_status = None
        self.fail_after_persist = False

    def handle(self, request):
        assert request.headers["authorization"] == "Bearer " + self.master
        assert request.headers["accept-encoding"] == "identity"
        self.calls.append((request.method, request.url.path))
        status, payload = 200, {}
        if request.url.path == "/key/info":
            assert request.method == "GET"
            digest = request.url.params["key"]
            assert re.fullmatch(r"[a-f0-9]{64}", digest)
            if self.lookup_status:
                status, payload = self.lookup_status, {"error": "synthetic-sensitive-error"}
            elif digest not in self.records:
                status, payload = self.missing_status, {"error": "key not found"}
            else:
                payload = {"info": deepcopy(self.records[digest])}
        elif request.url.path == "/key/generate":
            assert request.method == "POST"
            body = json.loads(request.content)
            assert "key_type" not in body, "llm_api key_type would expand the narrow route restriction"
            assert body["models"] == [ALIAS] and set(body["allowed_routes"]) == ROUTES
            digest = hashlib.sha256(body["key"].encode()).hexdigest()
            assert digest not in self.records, "Existing key must never be silently regenerated"
            self.records[digest] = {"models": body["models"], "allowed_routes": body["allowed_routes"],
                                    "blocked": False, "metadata": body["metadata"], "expires": None,
                                    "status": "active"}
            self.generated.append(deepcopy(body))
            if self.fail_after_persist:
                self.fail_after_persist = False
                status, payload = 503, {"error": "synthetic interrupted reply"}
            else:
                payload = {"key": body["key"]}
        else:
            pytest.fail("Unexpected management endpoint")
        return httpx.Response(status, request=request, stream=ReplyStream(payload))


def provisioner(monkeypatch, directory, missing_status=404):
    module = load_module("litellm_key_provisioner", "deploy/litellm/init_keys.py")
    master = read_env(directory / "admin.env")["LITELLM_MASTER_KEY"]
    registry = ManagementRegistry(master, missing_status)
    client = httpx.AsyncClient

    def factory(**kwargs):
        context = kwargs["verify"]
        assert isinstance(context, ssl.SSLContext) and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(registry.handle), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    return module, registry, master


@pytest.mark.parametrize("missing_status", [400, 404])
def test_provisioning_is_restart_safe_and_receipts_never_contain_plaintext_keys(private_config, monkeypatch, capsys, missing_status):
    module, registry, master = provisioner(monkeypatch, private_config, missing_status)
    module.provision(private_config, "https://litellm:4000", master)
    assert len(registry.generated) == 2
    original_receipts = {}
    for service in ("rags", "agentic"):
        receipt = private_config / (service + ".provisioned.json")
        original_receipts[service] = receipt.read_bytes()
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
        data = json.loads(receipt.read_bytes())
        assert data["service"] == service and data["model"] == ALIAS and set(data["allowed_routes"]) == ROUTES
        assert data["key_sha256"] == hashlib.sha256((private_config / service / "api.key").read_text().strip().encode()).hexdigest()
        assert (private_config / service / "api.key").read_text().strip() not in receipt.read_text()
    module.provision(private_config, "https://litellm:4000/", master)
    assert len(registry.generated) == 2
    assert all((private_config / (service + ".provisioned.json")).read_bytes() == content
               for service, content in original_receipts.items())
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("change", ["deleted", "blocked", "model_scope", "route_scope", "blocked_string", "blocked_integer",
                                    "expired", "invalid_expiry", "service_metadata", "routing_override", "inactive"])
def test_restart_refuses_revoked_or_changed_key_without_recreating_or_broadening(private_config, monkeypatch, change):
    module, registry, master = provisioner(monkeypatch, private_config)
    module.provision(private_config, "https://litellm:4000", master)
    digest = hashlib.sha256((private_config / "rags/api.key").read_text().strip().encode()).hexdigest()
    if change == "deleted":
        del registry.records[digest]
    elif change == "model_scope":
        registry.records[digest]["models"] = ["*"]
    elif change == "route_scope":
        registry.records[digest]["allowed_routes"].append("/key/generate")
    elif change in {"expired", "invalid_expiry"}:
        registry.records[digest]["expires"] = "2020-01-01T00:00:00+00:00" if change == "expired" else "invalid"
    elif change == "service_metadata":
        registry.records[digest]["metadata"]["service"] = "other-service"
    elif change == "routing_override":
        registry.records[digest]["router_settings"] = {"fallbacks": [{ALIAS: ["other-model"]}]}
    elif change == "inactive":
        registry.records[digest]["status"] = "inactive"
    else:
        registry.records[digest]["blocked"] = {"blocked": True, "blocked_string": "true", "blocked_integer": 1}[change]
    before = deepcopy(registry.records)
    receipt = (private_config / "rags.provisioned.json").read_bytes()
    with pytest.raises((module.ProvisionError, ValueError)):
        module.provision(private_config, "https://litellm:4000", master)
    assert registry.records == before and len(registry.generated) == 2
    assert (private_config / "rags.provisioned.json").read_bytes() == receipt


def test_interrupted_generate_response_recovers_by_lookup_without_duplicate_creation(private_config, monkeypatch):
    module, registry, master = provisioner(monkeypatch, private_config)
    registry.fail_after_persist = True
    with pytest.raises(httpx.HTTPStatusError):
        module.provision(private_config, "https://litellm:4000", master)
    assert len(registry.generated) == 1 and not (private_config / "rags.provisioned.json").exists()
    module.provision(private_config, "https://litellm:4000", master)
    assert len(registry.generated) == 2
    assert all((private_config / (service + ".provisioned.json")).is_file() for service in ("rags", "agentic"))


def test_partial_receipt_write_is_not_published_and_restart_reuses_remote_key(private_config, monkeypatch):
    module, registry, master = provisioner(monkeypatch, private_config)
    original_names = {path.name for path in private_config.iterdir()}

    def fail_write(_value, output, **_kwargs):
        output.write('{"schema_version":')
        output.flush()
        raise OSError("synthetic interrupted local receipt write")

    with monkeypatch.context() as fault:
        fault.setattr(module.json, "dump", fail_write)
        with pytest.raises(OSError, match="interrupted local receipt"):
            module.provision(private_config, "https://litellm:4000", master)
    assert len(registry.generated) == 1
    assert not list(private_config.glob("*.provisioned.json"))
    assert {path.name for path in private_config.iterdir()} == original_names
    module.provision(private_config, "https://litellm:4000", master)
    assert len(registry.generated) == 2
    assert all((private_config / (service + ".provisioned.json")).is_file() for service in ("rags", "agentic"))


def test_exclusive_receipt_publish_preserves_concurrently_created_file(tmp_path, monkeypatch):
    module = load_module("litellm_receipt_publish", "deploy/litellm/init_keys.py")
    receipt = tmp_path / "rags.provisioned.json"
    original_link = module.os.link

    def raced_link(source, destination, **kwargs):
        Path(destination).write_text("synthetic-concurrent-operator-receipt")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        module.save_receipt(receipt, {"schema_version": 1})
    assert receipt.read_text() == "synthetic-concurrent-operator-receipt"
    assert list(tmp_path.iterdir()) == [receipt]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_failed_lookup_never_attempts_key_generation(private_config, monkeypatch, status):
    module, registry, master = provisioner(monkeypatch, private_config)
    registry.lookup_status = status
    with pytest.raises(module.ProvisionError, match="refused service-key lookup"):
        module.provision(private_config, "https://litellm:4000", master)
    assert not registry.generated and not list(private_config.glob("*.provisioned.json"))


@pytest.mark.parametrize("kind", ["key_symlink", "receipt_symlink", "changed_receipt", "oversized_receipt"])
def test_unsafe_local_key_or_receipt_fails_before_management_calls(private_config, tmp_path, monkeypatch, kind):
    module, registry, master = provisioner(monkeypatch, private_config)
    key = private_config / "rags/api.key"
    receipt = private_config / "rags.provisioned.json"
    preserved = tmp_path / "preserved"
    preserved.write_text("synthetic-preserved")
    if kind == "key_symlink":
        key.unlink()
        key.symlink_to(preserved)
    elif kind == "receipt_symlink":
        receipt.symlink_to(preserved)
    else:
        receipt.write_text("x" * 4097 if kind == "oversized_receipt" else '{"schema_version":999}')
    with pytest.raises((ValueError, module.ProvisionError)):
        module.provision(private_config, "https://litellm:4000", master)
    assert not registry.calls and not registry.generated
    assert preserved.read_text() == "synthetic-preserved"


def test_key_initialization_cli_does_not_echo_sensitive_upstream_error(private_config, monkeypatch, capsys):
    module, registry, master = provisioner(monkeypatch, private_config)
    registry.lookup_status = 503
    monkeypatch.setenv("LITELLM_MASTER_KEY", master)
    monkeypatch.setattr(sys, "argv", ["init_keys.py", "--directory", str(private_config)])
    with pytest.raises(SystemExit) as stopped:
        module.main()
    captured = capsys.readouterr()
    output = str(stopped.value) + captured.out + captured.err
    assert "Gateway key initialization failed" in output
    assert master not in output and "synthetic-sensitive-error" not in output
    assert not registry.generated


def test_static_gateway_configuration_pins_runtime_and_disables_hidden_retry_and_content_logging():
    runtime = json.loads((ROOT / "deploy/litellm/runtime.json").read_text())
    services = yaml.safe_load((ROOT / "deploy/litellm/compose.yaml").read_text())["services"]
    proxy = yaml.safe_load((ROOT / "deploy/litellm/proxy.yaml").read_text())
    assert re.fullmatch(r"ghcr\.io/berriai/litellm:v1\.110\.2@sha256:[a-f0-9]{64}", runtime["image"])
    assert services["litellm"]["image"] == runtime["image"]
    assert runtime["version"] == "1.110.2"
    package = runtime["python_package"]
    assert re.fullmatch(r"litellm\[[a-z_,]+\]==1\.110\.2", package)
    assert "proxy" in package.split("[", 1)[1].split("]", 1)[0].split(",")
    assert len(proxy["model_list"]) == 1 and proxy["model_list"][0]["model_name"] == ALIAS
    assert proxy["model_list"][0]["litellm_params"]["max_retries"] == 0
    assert proxy["model_list"][0]["model_info"]["mode"] == "chat"
    assert proxy["router_settings"]["num_retries"] == 0 and proxy["router_settings"]["max_fallbacks"] == 0
    assert proxy["router_settings"]["fallbacks"] == [] and proxy["router_settings"]["cache_responses"] is False
    settings = proxy["litellm_settings"]
    assert settings["num_retries"] == 0
    assert all(settings[name] is False for name in ("drop_params", "modify_params", "cache", "set_verbose", "telemetry"))
    assert settings["turn_off_message_logging"] is True
    assert proxy["general_settings"]["store_prompts_in_spend_logs"] is False
    assert proxy["general_settings"]["disable_error_logs"] is True
    assert proxy["general_settings"]["background_health_checks"] is False
    assert services["litellm"]["ports"] == ["127.0.0.1:4000:4000"]
    assert not services["litellm-db"].get("ports")
    assert all(service["profiles"] == ["litellm"] for service in services.values())
    assert services["litellm-init"]["depends_on"]["litellm"]["condition"] == "service_healthy"
    initializer = services["litellm-init"]
    assert initializer["user"] == "0:0" and initializer["restart"] == "no"
    assert initializer["cap_drop"] == ["ALL"] and initializer["cap_add"] == ["DAC_OVERRIDE"]
    assert initializer["security_opt"] == ["no-new-privileges:true"]
    assert initializer["env_file"] == "../../data/litellm/admin.env"
    assert initializer["volumes"] == [
        "../../data/litellm/receipts:/bootstrap",
        "../../data/litellm/ca.pem:/bootstrap/ca.pem:ro",
        "../../data/litellm/rags/api.key:/bootstrap/rags/api.key:ro",
        "../../data/litellm/agentic/api.key:/bootstrap/agentic/api.key:ro",
        "./init_keys.py:/bootstrap-tools/init_keys.py:ro",
    ]
    assert not services["litellm"].get("cap_add")


def test_static_overlay_mounts_only_own_key_and_ca_and_keeps_verified_generation_routes():
    overlay = yaml.safe_load((ROOT / "compose.litellm.yaml").read_text())
    baseline = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert overlay["include"] == ["deploy/litellm/compose.yaml"]
    assert set(overlay["services"]) == {"rags", "api", "worker"}
    for name, service in overlay["services"].items():
        owner = "rags" if name == "rags" else "agentic"
        assert service["volumes"] == [f"./data/litellm/{owner}:/app/gateway:ro"]
        assert service["depends_on"]["litellm-init"]["condition"] == "service_completed_successfully"
        env = {**baseline["services"][name]["environment"], **service["environment"]}
        assert not ({"LITELLM_MASTER_KEY", "DATABASE_URL", "INKLING_UPSTREAM_API_KEY"} & env.keys())
        assert not service.get("env_file")
        assert "admin.env" not in json.dumps(service) and "server.key" not in json.dumps(service)
        if name == "rags":
            assert env["RAGS_MODEL_PROVIDER"] == "router"
            assert env["RAGS_ROUTER_GATEWAY_URL"] == "https://litellm:4000/v1"
            assert env["RAGS_ROUTER_AUTH_MODE"] == "required" and env["RAGS_ROUTER_ALLOW_HTTP"] == "false"
            assert env["RAGS_ROUTER_API_KEY"] == "" and env["RAGS_ROUTER_API_KEY_FILE"] == "/app/gateway/api.key"
            assert env["RAGS_ROUTER_CA_FILE"] == "/app/gateway/ca.pem"
            assert "RAGS_ENV" not in service["environment"] and "RAGS_EMBEDDING_PROVIDER" not in service["environment"]
            assert json.loads(env["RAGS_ROUTER_MODELS_JSON"]) == {"inkling": ALIAS}
        else:
            routes = json.loads(env["AGENT_MODEL_ROUTES_JSON"])
            assert len(routes) == 1 and routes[0]["model"] == ALIAS
            assert routes[0]["base_url"] == "https://litellm:4000/v1" and routes[0]["auth_mode"] == "required"
            assert routes[0]["ca_file"] == "/app/gateway/ca.pem" and routes[0]["output_token_parameter"] == "max_tokens"
            assert routes[0]["api_key_env"] == "AGENT_GATEWAY_API_KEY"
            assert env["AGENT_GATEWAY_API_KEY"] == "" and env["AGENT_GATEWAY_API_KEY_FILE"] == "/app/gateway/api.key"
            assert "AGENT_ENV" not in service["environment"]
