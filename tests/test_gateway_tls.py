"""Verified private-CA HTTPS routes without disabling TLS or using environment proxies."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agentic.config import ModelRoute, Settings, model_routes, model_ssl_context
from agentic.model import ModelGateway
from agentic.store import Store


@pytest.fixture
def certificate(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopback-fixture")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "fixture-ca.pem", tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


@pytest.mark.unit
def test_selected_ca_loads_verified_context_and_round_trips_route_configuration(certificate, monkeypatch):
    ca, _ = certificate
    context = model_ssl_context(str(ca))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "name": "gateway-v1", "provider": "openai", "model": "inkling/v1",
        "base_url": "https://gateway.test/v1", "ca_file": str(ca), "api_key_env": "GATEWAY_FIXTURE_KEY",
    }]))
    monkeypatch.setenv("GATEWAY_FIXTURE_KEY", "synthetic-gateway-key")
    route, = model_routes("offline")
    assert route.ca_file == str(ca) and route.auth_mode == "required"
    with pytest.raises(ValueError, match="requires HTTPS"):
        replace(route, base_url="http://gateway.test/v1")


@pytest.mark.unit
def test_ca_symlink_is_rejected_even_when_its_target_is_a_valid_certificate(certificate):
    ca, _ = certificate
    link = ca.with_name("symlink-ca.pem")
    link.symlink_to(ca)
    with pytest.raises(ValueError, match="CA file"):
        model_ssl_context(str(link))


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "oversized", "malformed", "fifo"])
def test_invalid_ca_files_fail_without_hanging_or_loading_credentials(tmp_path, monkeypatch, kind):
    ca = tmp_path / "invalid.pem"
    if kind == "directory":
        ca.mkdir()
    elif kind == "fifo":
        os.mkfifo(ca)
    elif kind != "missing":
        ca.write_bytes({"empty": b"", "oversized": b"x" * 1_048_577, "malformed": b"not a certificate"}[kind])
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "provider": "openai", "base_url": "https://gateway.test/v1", "ca_file": str(ca),
    }]))
    loads = []
    with pytest.raises(ValueError, match="CA file"):
        model_routes("offline", credential_loader=lambda name: loads.append(name))
    assert loads == []


@pytest.mark.integration
async def test_real_tls_gateway_accepts_only_selected_ca_and_bypasses_ambient_proxies(certificate, tmp_path, monkeypatch):
    ca, key = certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, key)
    calls, handlers = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if line)
            payload = json.loads(await reader.readexactly(int(headers["Content-Length"])))
            calls.append((headers, payload))
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "TLS fixture"}}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    endpoint = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    route = ModelRoute("gateway-v1", "openai", "inkling/v1", endpoint, "synthetic-service-key", ca_file=str(ca))
    settings = Settings(db_path=str(tmp_path / "tls.db"), keys={"fixture": "tenant"}, rag_url="http://127.0.0.1:1",
                        rag_keys={"tenant": "fixture-rag"}, provider="routed", openai_key="", openai_model="",
                        openai_base_url="https://example.invalid", anthropic_key="", anthropic_model="",
                        max_parallel=1, routes=(route,))
    try:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        model = ModelGateway(settings, Store(settings.db_path))
        assert await model.complete("System", "Question", tenant="tenant") == "TLS fixture"
        assert len(calls) == 1
        assert calls[0][0]["Authorization"] == "Bearer synthetic-service-key"
        assert calls[0][1]["model"] == "inkling/v1"
        # A transport without the selected trust root must reject the same
        # self-signed certificate, even though the destination is loopback.
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(endpoint)
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)
