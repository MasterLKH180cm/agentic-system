"""Create private gateway configuration, certificates and separate service keys.

This installs no runtime and makes no model requests. Use the generated upstream
key with the separately provisioned inference server. Existing files are never
overwritten. The private CA is scoped to this gateway, not installed globally.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ALIAS = "gateway/inkling-2-mini-iq4nl-v1"


def write(path: Path, content: str, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        output.write(content)


def create(directory: Path, upstream_url="http://host.docker.internal:8080/v1") -> list[str]:
    parsed = urlsplit(upstream_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or "\n" in upstream_url or "\r" in upstream_url):
        raise ValueError("Upstream must be an explicit HTTP(S) model endpoint without credentials or query")
    if not shutil.which("openssl"):
        raise ValueError("OpenSSL is required to generate the private gateway certificate")
    if directory.exists() or directory.is_symlink():
        raise FileExistsError("Existing gateway configuration kept; choose a new directory")
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    try:
        # The initializer mounts only this private writable directory plus its
        # CA and service keys as individual read-only files.
        (directory / "receipts").mkdir(mode=0o700)
        (directory / "receipts").chmod(0o700)
        master, salt, password, upstream = ("sk-" + secrets.token_urlsafe(40) for _ in range(4))
        for service in ("rags", "agentic"):
            (directory / service).mkdir(mode=0o755)
            (directory / service).chmod(0o755)
            # The parent is host-private (0700); each service mounts only its own
            # directory, read-only, and runs under its existing container UID.
            write(directory / service / "api.key", "sk-" + secrets.token_urlsafe(40) + "\n", 0o644)
        write(directory / "db.env", "POSTGRES_USER=litellm\nPOSTGRES_DB=litellm\nPOSTGRES_PASSWORD=" + password + "\n")
        write(directory / "admin.env", "LITELLM_MASTER_KEY=" + master + "\n")
        write(directory / "upstream.env", "INKLING_UPSTREAM_API_KEY=" + upstream + "\n")
        write(directory / "upstream.key", upstream + "\n")
        write(directory / "proxy.env", "\n".join([
            "LITELLM_MASTER_KEY=" + master, "LITELLM_SALT_KEY=" + salt,
            "DATABASE_URL=postgresql://litellm:" + password + "@litellm-db:5432/litellm",
            "INKLING_UPSTREAM_API_BASE=" + upstream_url.rstrip("/"), "INKLING_UPSTREAM_API_KEY=" + upstream,
            "LITELLM_LOCAL_MODEL_COST_MAP=True", "LITELLM_TELEMETRY=False", "DO_NOT_TRACK=1",
            "LITELLM_LOG=ERROR", "STORE_MODEL_IN_DB=False", "",
        ]))
        write(directory / "proxy.yaml", (ROOT / "deploy/litellm/proxy.yaml").read_text(), 0o644)
        commands = [
            ["req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "365", "-sha256",
             "-subj", "/CN=RAG Agentic Private Gateway CA", "-keyout", "ca.key", "-out", "ca.pem",
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            ["req", "-newkey", "rsa:3072", "-nodes", "-sha256", "-subj", "/CN=litellm",
             "-keyout", "server.key", "-out", "server.csr"],
            ["x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-days", "90", "-sha256", "-extfile", "server.ext", "-out", "server.pem"],
        ]
        write(directory / "server.ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:litellm,DNS:localhost,IP:127.0.0.1,IP:::1\n")
        for command in commands:
            subprocess.run(["openssl", *command], cwd=directory, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        (directory / "ca.key").chmod(0o600)
        for filename in ("server.key", "server.pem", "ca.pem"):
            # Only individual files are mounted into the gateway. The containing
            # host directory stays private; no application sees server.key.
            (directory / filename).chmod(0o644)
        for service in ("rags", "agentic"):
            write(directory / service / "ca.pem", (directory / "ca.pem").read_text(), 0o644)
        routes = [{"name": "inkling-gateway-v1", "provider": "openai", "model": ALIAS,
                   "base_url": "https://litellm:4000/v1", "api_key_env": "AGENT_GATEWAY_API_KEY",
                   "ca_file": "/app/gateway/ca.pem", "auth_mode": "required",
                   "output_token_parameter": "max_tokens", "purposes": ["planning", "synthesis"]}]
        write(directory / "routes.json", json.dumps(routes, indent=2) + "\n", 0o644)
        write(directory / "manifest.json", json.dumps({"schema_version": 1, "model_alias": ALIAS,
              "gateway_version": "1.110.2", "upstream_endpoint_configured": True,
              "inference_executed": False, "tls_server_valid_days": 90}, indent=2) + "\n", 0o644)
        for filename in ("server.csr", "server.ext", "ca.srl"):
            (directory / filename).unlink()
    except BaseException:
        shutil.rmtree(directory)
        raise
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "data/litellm")
    parser.add_argument("--upstream-url", default="http://host.docker.internal:8080/v1")
    args = parser.parse_args()
    try:
        names = create(args.directory.absolute(), args.upstream_url)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Gateway configuration was not created; check the target path, URL and OpenSSL") from None
    print(f"Created {len(names)} gateway configuration files. No model was downloaded or called.")
    print("Configure the inference server with the private upstream.key; see docs/LITELLM.md.")


if __name__ == "__main__":
    main()
