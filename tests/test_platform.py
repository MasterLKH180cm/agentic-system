"""Real cryptographic, HTTP, streaming and optional datastore integration checks."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
import jwt
import pytest

from agentic.api import create_app
from agentic.auth import Authenticator, RateLimiter
from agentic.config import Settings
from agentic.schemas import Step, WorkflowIn
from agentic.store import Store
from agentic.storage_adapters import ArtifactStore


@contextmanager
def http_server(get_body=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(get_body or {}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def platform_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_RATE_DB_PATH", str(tmp_path / "limits.db"))
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("AGENT_REDIS_URL", raising=False)
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    return replace(Settings.from_env(), db_path=str(tmp_path / "workflows.db"))


def test_verified_oidc_roles_attributes_and_wrong_claims(platform_config, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "integration-key", "use": "sig", "alg": "RS256"})
    with http_server({"keys": [jwk]}) as (url, _):
        monkeypatch.setenv("AGENT_OIDC_ISSUER", url)
        monkeypatch.setenv("AGENT_OIDC_AUDIENCE", "agent-test")
        monkeypatch.setenv("AGENT_OIDC_JWKS_URL", url + "/jwks")
        auth = Authenticator(platform_config)
        claims = {"iss": url, "aud": "agent-test", "sub": "alice", "iat": int(time.time()),
                  "exp": int(time.time()) + 60, "tenant_id": "local", "roles": ["requester"],
                  "memory_namespaces": ["allowed"]}
        def token(data):
            return "Bearer " + jwt.encode(data, private_key, algorithm="RS256", headers={"kid": "integration-key"})
        principal = auth.authenticate(token(claims))
        auth.require(principal, "workflow:write")
        auth.require(principal, "memory:read", {"namespace": "allowed"})
        for resource in [{"tenant": "foreign"}, {"namespace": "secret"}]:
            with pytest.raises(HTTPException) as err:
                auth.require(principal, "memory:read", resource)
            assert err.value.status_code == 403
        for change in [{"aud": "wrong"}, {"iss": "https://wrong.invalid"},
                       {"exp": int(time.time()) - 1}, {"tenant_id": "foreign"}, {"roles": "requester"},
                       {"roles": ["requester", "approver"]}, {"sub": ""}]:
            with pytest.raises(HTTPException) as err:
                auth.authenticate(token({**claims, **change}))
            assert err.value.status_code == 401
        with pytest.raises(HTTPException):
            auth.require(principal, "workflow:approve")
        # Scope restrictions cannot be bypassed by a broad requester role.
        reader = auth.authenticate(token({**claims, "scope": "workflow:read"}))
        with pytest.raises(HTTPException):
            auth.require(reader, "workflow:write")


def test_hmac_replay_sse_cursor_and_dashboard(platform_config, monkeypatch):
    secret = "local-test-webhook-secret-at-least-24"
    monkeypatch.setenv("AGENT_WEBHOOKS_JSON", json.dumps({"fixture": {"tenant": "local", "secret": secret}}))
    app = create_app(platform_config)
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "sha256-" in page.headers["content-security-policy"]
    assert "local-agent-key" not in page.text
    body = json.dumps({"goal": "Calculate 6 * 7", "idempotency_key": "logical-event", "steps": [{"id": "calculate", "tool": "math.calculate",
                                                          "args": {"expression": "6 * 7"}}]}).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {"X-Agent-Hook": "fixture", "X-Agent-Timestamp": stamp,
               "X-Agent-Signature": "sha256=" + signature, "Content-Type": "application/json"}
    assert client.post("/hooks/workflows", content=body + b" ", headers=headers).status_code == 401
    accepted = client.post("/hooks/workflows", content=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert client.post("/hooks/workflows", content=body, headers=headers).status_code == 409
    rid = accepted.json()["id"]
    new_stamp = str(int(stamp) + 1)
    new_signature = hmac.new(secret.encode(), new_stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    retried = client.post("/hooks/workflows", content=body, headers={**headers,
                          "X-Agent-Timestamp": new_stamp, "X-Agent-Signature": "sha256=" + new_signature})
    assert retried.status_code == 202 and retried.json() == {"id": rid, "duplicate": True}
    store = app.state.store
    with store.connect() as db:
        for index in range(1005):
            store.event(db, "local", rid, None, "cursor_fixture", {"number": index})
        db.execute("UPDATE runs SET status='completed' WHERE id=?", (rid,))
    cursor = store.events("local", rid, 0, 1000)[-1]["id"]
    response = client.get(f"/workflows/{rid}/events/stream", headers={
        "Authorization": "Bearer local-agent-key", "Last-Event-ID": str(cursor)})
    assert response.status_code == 200
    assert "event: done" in response.text and '"number": 1004' in response.text
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids and min(ids) > cursor


def test_shared_rate_limit_and_artifact_isolation(platform_config, tmp_path):
    with pytest.raises(HTTPException) as error:
        Authenticator(platform_config).authenticate("Bearer é")
    assert error.value.status_code == 401
    first, second = RateLimiter(str(tmp_path / "shared.db"), 2), RateLimiter(str(tmp_path / "shared.db"), 2)
    assert first.take("a") and second.take("a") and not first.take("a")
    assert second.take("b")
    artifacts = ArtifactStore()
    reference = artifacts.put("tenant-a", {"result": 42})
    assert artifacts.get("tenant-a", reference["artifact_id"]) == {"result": 42}
    assert artifacts.get("tenant-b", reference["artifact_id"]) is None
    with pytest.raises(ValueError):
        artifacts.get("tenant-a", "../../outside")


def test_otel_actual_http_export_has_no_content(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from agentic.telemetry import Telemetry
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    with http_server() as (url, received):
        monkeypatch.setenv("AGENT_TELEMETRY_ENABLED", "1")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
        telemetry = Telemetry()
        try:
            with pytest.raises(RuntimeError):
                with telemetry.span("fixture.operation", {"run.id": "run-fixture", "prompt": "secret-source-text"}):
                    raise RuntimeError("secret-provider-token")
            telemetry.flush()
        finally:
            telemetry.shutdown()
        traces = [body for path, _, body in received if path == "/v1/traces"]
        assert traces
        decoded = ExportTraceServiceRequest.FromString(traces[0])
        rendered = str(decoded)
        assert "fixture.operation" in rendered and "run-fixture" in rendered
        assert "secret-source-text" not in rendered and "secret-provider-token" not in rendered


@pytest.mark.skipif(not os.getenv("AGENT_TEST_POSTGRES_URL"), reason="Actual PostgreSQL service not configured")
def test_real_postgres_workflow_state_and_fencing():
    tenant = "test-" + uuid.uuid4().hex
    first = Store(os.environ["AGENT_TEST_POSTGRES_URL"])
    second = Store(os.environ["AGENT_TEST_POSTGRES_URL"])
    run = first.submit(tenant, WorkflowIn(goal="Compute a value", steps=[Step(id="calc", tool="math.calculate",
                                                                         args={"expression": "2+3"})]))
    try:
        job = first.claim()
        assert job["run_id"] == run["id"]
        assert second.claim() is None
        assert first.heartbeat(job)
        assert first.checkpoint(job, {"round": 1})
        assert second.get(tenant, run["id"])["steps"][0]["checkpoint"]["round"] == 1
        reservation = first.reserve_usage(tenant, run["id"], "integration", "synthesis", 100, 1000)
        ready = threading.Barrier(8)

        def settle(index):
            ready.wait(timeout=10)
            store = first if index % 2 == 0 else second
            store.settle_usage(reservation, input_tokens=index + 1, output_tokens=2,
                              cost_microusd=index + 10)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(settle, range(8)))
        record, = first.usage(tenant, run["id"])["calls"]
        assert record["status"] == "reported"
        assert record["charged_tokens"] == record["input_tokens"] + 2
        assert record["cost_microusd"] == record["input_tokens"] + 9
        second.settle_usage(reservation, input_tokens=99, output_tokens=99, cost_microusd=99)
        assert first.usage(tenant, run["id"])["calls"] == [record]
        events = [event for event in first.events(tenant, run["id"]) if event["type"] == "model_usage"]
        assert len(events) == 1 and events[0]["payload"]["input_tokens"] == record["input_tokens"]
        assert first.complete(job, {"value": 5})
        assert not second.complete(job, {"value": 99})
        assert second.get(tenant, run["id"])["status"] == "completed"
    finally:
        with first.connect() as db:
            db.execute("DELETE FROM model_usage WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM events WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM steps WHERE run_id=?", (run["id"],))
            db.execute("DELETE FROM runs WHERE tenant=?", (tenant,))


@pytest.mark.skipif(not os.getenv("AGENT_TEST_REDIS_URL"), reason="Actual Redis service not configured")
def test_real_redis_limits_are_shared_and_expiring(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REDIS_URL", os.environ["AGENT_TEST_REDIS_URL"])
    first = RateLimiter(str(tmp_path / "unused.db"), 2)
    second = RateLimiter(str(tmp_path / "unused-other.db"), 2)
    bucket = "integration-" + uuid.uuid4().hex
    assert first.take(bucket) and second.take(bucket) and not first.take(bucket)
    digest = hashlib.sha256(bucket.encode()).hexdigest()
    keys = list(first.redis.scan_iter(match="agent:limit:*:" + digest))
    assert len(keys) == 1 and 0 < first.redis.ttl(keys[0]) <= 120
    first.redis.delete(*keys)


@pytest.mark.skipif(not os.getenv("AGENT_TEST_S3_ENDPOINT"), reason="Actual S3-compatible service not configured")
def test_real_s3_artifacts_are_isolated_and_integrity_checked(monkeypatch):
    import boto3
    from botocore.config import Config
    endpoint = os.environ["AGENT_TEST_S3_ENDPOINT"]
    bucket = "agent-test-" + uuid.uuid4().hex
    client = boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "path"}))
    client.create_bucket(Bucket=bucket)
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_ARTIFACT_BACKEND", "s3")
    monkeypatch.setenv("AGENT_S3_ENDPOINT", endpoint)
    monkeypatch.setenv("AGENT_S3_BUCKET", bucket)
    artifacts = ArtifactStore()
    try:
        reference = artifacts.put("tenant-a", {"result": 42, "provenance": "synthetic"})
        assert reference["storage"] == "s3"
        assert artifacts.get("tenant-a", reference["artifact_id"])["result"] == 42
        assert artifacts.get("tenant-b", reference["artifact_id"]) is None
        client.put_object(Bucket=bucket, Key=artifacts._key("tenant-a", reference["artifact_id"]), Body=b"{}")
        with pytest.raises(ValueError, match="integrity"):
            artifacts.get("tenant-a", reference["artifact_id"])
    finally:
        for item in client.list_objects_v2(Bucket=bucket).get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)
