"""Provision two narrowly scoped service keys; never print credentials.

Run after the gateway database and verified TLS endpoint are ready. Locally
generated keys are reused across restarts, but a provisioned key that has been
removed or blocked is never silently re-created or unblocked.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile
import urllib.parse

import httpx

ALIAS = "gateway/inkling-2-mini-iq4nl-v1"
MODEL_ALIASES = (ALIAS, "gateway/qwen3-1.7b-q8_0-v1")
ROUTES = ["/chat/completions", "/v1/chat/completions", "/models", "/v1/models"]


class ProvisionError(RuntimeError):
    pass


def expired(value) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        raise ProvisionError("Invalid service-key expiration")
    deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= datetime.now(timezone.utc)


def save_receipt(path: Path, payload: dict) -> None:
    temporary = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".key-receipt-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Publish complete bytes without replacing a concurrently created receipt.
        os.link(temporary, path, follow_symlinks=False)
        published = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if published:
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def request(base: str, path: str, master: str, context: ssl.SSLContext, payload=None):
    async def bounded():
        # One deadline covers connection, TLS, headers, body and cleanup.
        async with asyncio.timeout(10), httpx.AsyncClient(
            verify=context, trust_env=False, follow_redirects=False, timeout=10,
        ) as client:
            async with client.stream("GET" if payload is None else "POST", base + path,
                json=payload, headers={"Authorization": "Bearer " + master,
                                      "Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProvisionError("Compressed management replies are not accepted")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=8192):
                    if len(chunk) > 65536 - len(body):
                        raise ProvisionError("Gateway management reply exceeded the limit")
                    body.extend(chunk)
                return json.loads(body)
    return asyncio.run(bounded())


def provision(directory: Path, base: str, master: str, *, model_alias: str = ALIAS) -> None:
    if model_alias not in MODEL_ALIASES:
        raise ValueError("Unsupported generation deployment alias")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Gateway management URL must be a clean HTTPS origin")
    if not isinstance(master, str) or not 32 <= len(master) <= 4096:
        raise ValueError("Gateway administrative credential is missing or invalid")
    context = ssl.create_default_context(cafile=directory / "ca.pem")
    for service in ("rags", "agentic"):
        key_file = directory / service / "api.key"
        if key_file.is_symlink() or not key_file.is_file() or key_file.stat().st_size > 4096:
            raise ValueError("Missing regular service credential file")
        key = key_file.read_text().strip()
        if not key.startswith("sk-") or not 40 <= len(key) <= 4096:
            raise ValueError("Invalid service credential")
        receipt = directory / (service + ".provisioned.json")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        expected_receipt = {"schema_version": 1, "service": service, "key_sha256": key_hash,
                            "model": model_alias, "allowed_routes": ROUTES}
        if receipt.is_symlink():
            raise ProvisionError("Provisioning receipt cannot be a symlink")
        if receipt.exists() and (not receipt.is_file() or receipt.stat().st_size > 4096
                                 or json.loads(receipt.read_bytes()) != expected_receipt):
            raise ProvisionError("Provisioning receipt changed; explicit operator repair is required")
        info = None
        try:
            info = request(base.rstrip("/"), "/key/info?key=" + key_hash, master, context)["info"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {400, 404}:
                raise ProvisionError("Gateway refused service-key lookup") from None
        if info is None:
            if receipt.exists() or receipt.is_symlink():
                raise ProvisionError("Previously provisioned key is absent; explicit operator repair is required")
            request(base.rstrip("/"), "/key/generate", master, context, {
                "key": key, "key_alias": service + "-generation-v1", "models": [model_alias],
                # key_type=llm_api expands to all inference APIs in 1.110.2 and
                # would overwrite this narrower explicit endpoint allowlist.
                "allowed_routes": ROUTES,
                "metadata": {"service": service, "deployment_contract": "generation-v1"},
            })
            info = request(base.rstrip("/"), "/key/info?key=" + key_hash, master, context)["info"]
        expires = info.get("expires") if isinstance(info, dict) else None
        if (not isinstance(info, dict) or info.get("status") != "active"
                or (info.get("blocked") is not None and info.get("blocked") is not False) or expired(expires)
                or info.get("models") != [model_alias] or set(info.get("allowed_routes") or []) != set(ROUTES)
                or any(info.get(field) for field in ("aliases", "config", "router_settings"))
                or info.get("metadata") != {"service": service, "deployment_contract": "generation-v1"}):
            raise ProvisionError("Existing service key has a different or blocked policy; explicit repair is required")
        if not receipt.exists():
            save_receipt(receipt, expected_receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/bootstrap"))
    parser.add_argument("--base-url", default="https://litellm:4000")
    parser.add_argument("--model-alias", choices=MODEL_ALIASES,
                        default=os.environ.get("LITELLM_MODEL_ALIAS", ALIAS))
    args = parser.parse_args()
    try:
        provision(args.directory, args.base_url, os.environ.get("LITELLM_MASTER_KEY", ""),
                  model_alias=args.model_alias)
    except (OSError, ValueError, KeyError, TypeError, ProvisionError, TimeoutError, httpx.HTTPError):
        # Management errors can contain key/DSN data. Never print their bodies.
        raise SystemExit("Gateway key initialization failed; inspect configuration and administrative key status") from None
    print("Gateway service keys verified; model and endpoint restrictions are active.")


if __name__ == "__main__":
    main()
