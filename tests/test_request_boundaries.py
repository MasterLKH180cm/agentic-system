"""Rejected request data stays readable and cannot alter durable state."""
from contextlib import closing
from dataclasses import replace
import hashlib
import hmac
import json
import sqlite3
import time

import httpx
import pytest

from agentic.api import create_app
from agentic.config import Settings
from agentic.schemas import MemoryIn


@pytest.fixture
def boundary_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"fixture-requester":"fixture"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"fixture":"fixture-rag"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"fixture-approver":"fixture"}')
    monkeypatch.setenv("AGENT_RATE_DB_PATH", str(tmp_path / "limits.db"))
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENT_WEBHOOKS_JSON", json.dumps({
        "fixture": {"tenant": "fixture", "secret": "disposable-webhook-boundary-fixture"},
    }))
    for key in ("AGENT_REDIS_URL", "AGENT_DATABASE_URL", "AGENT_OIDC_ISSUER",
                "AGENT_OIDC_AUDIENCE", "AGENT_OIDC_JWKS_URL"):
        monkeypatch.delenv(key, raising=False)
    return create_app(replace(Settings.from_env(), db_path=str(tmp_path / "workflows.db")))


def assert_validation(response):
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/json")
    details = response.json()["detail"]
    assert details and all(set(item) == {"type", "loc", "msg"} for item in details)
    # Every returned string is valid UTF-8, including attacker-controlled paths.
    json.dumps(details, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert "private-rejected-value" not in response.text
    return details


@pytest.mark.integration
@pytest.mark.parametrize("field,value", [
    (b"x-agent-hook", b"unknown"),
    (b"x-agent-timestamp", b""),
    (b"x-agent-timestamp", b"-1"),
    (b"x-agent-timestamp", b"1.5"),
    (b"x-agent-timestamp", b"1234567890123"),
    (b"x-agent-timestamp", b"\xb2"),
    (b"x-agent-timestamp", b"0"),
    (b"x-agent-signature", b""),
    (b"x-agent-signature", b"sha256=\xe9"),
    (b"x-agent-signature", b"SHA256=" + b"0" * 64),
    (b"x-agent-signature", b"sha256=" + b"g" * 64),
    (b"x-agent-signature", b"sha256=" + b"0" * 63),
    (b"x-agent-signature", b"sha256=" + b"0" * 65),
    (b"x-agent-signature", b"sha256=" + b"0" * 64),
])
async def test_invalid_webhook_headers_return_401_without_side_effects(boundary_app, field, value):
    body = b'{"goal":"Calculate 6 * 7"}'
    timestamp = str(int(time.time())).encode("ascii")
    digest = hmac.new(b"disposable-webhook-boundary-fixture", timestamp + b"." + body,
                      hashlib.sha256).hexdigest().encode("ascii")
    headers = {b"x-agent-hook": b"fixture", b"x-agent-timestamp": timestamp,
               b"x-agent-signature": b"sha256=" + digest, b"content-type": b"application/json"}
    headers[field] = value
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary_app, raise_app_exceptions=False),
                                 base_url="http://fixture.test") as client:
        response = await client.post("/hooks/workflows", content=body, headers=headers)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] in {
        "Invalid webhook authentication", "Expired webhook timestamp", "Invalid webhook signature",
    }
    assert boundary_app.state.store.list("fixture") == []
    with boundary_app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM hook_receipts").fetchone()[0] == 0
    with closing(sqlite3.connect(boundary_app.state.authenticator.limiter.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM request_limits").fetchone()[0] == 0


@pytest.mark.integration
@pytest.mark.parametrize("body,location", [
    (b'{"goal":"private-rejected-value","max_cost_usd":1e309}', ["body", "max_cost_usd"]),
    (b'{"goal":"private-rejected-value","max_cost_usd":NaN}', ["body", "max_cost_usd"]),
    (b'{"goal":"\\ud800xx"}', ["body", "goal"]),
    (b'{"goal":"Calculate 6 * 7","\\udfff":"private-rejected-value"}', ["body"]),
    (json.dumps({"goal": "Calculate 6 * 7", "未知欄位": "private-rejected-value"}).encode("utf-8"),
     ["body", "未知欄位"]),
    (b'{"goal":""}', ["body", "goal"]),
    (b'{"goal":', ["body", 8]),
])
async def test_invalid_workflows_return_safe_details_without_persistence(boundary_app, body, location):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary_app, raise_app_exceptions=False),
                                 base_url="http://fixture.test") as client:
        response = await client.post("/workflows", content=body, headers={
            "Authorization": "Bearer fixture-requester", "Content-Type": "application/json",
        })
    details = assert_validation(response)
    assert details[0]["loc"] == location
    assert boundary_app.state.store.list("fixture") == []
    with boundary_app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 0


INVALID_METADATA = [
    {"amount": float("inf")}, {"amount": float("nan")},
    {"nested": {"label": "\ud800"}}, {"nested": ["\udfff"]},
    {"\ud800": "private-rejected-value"},
]


@pytest.mark.unit
@pytest.mark.parametrize("metadata", INVALID_METADATA)
def test_memory_metadata_rejects_values_that_cannot_round_trip(metadata):
    with pytest.raises(ValueError):
        MemoryIn(content="Retain usable memory", metadata=metadata)


@pytest.mark.unit
def test_valid_unicode_memory_preserves_metadata_and_existing_size_limit():
    metadata = {"標籤": ["測試 🧠", {"café": "e\u0301"}], "count": 0}
    value = MemoryIn(content="保留原始內容 🧠", metadata=metadata)
    assert value.metadata == metadata and value.content == "保留原始內容 🧠"
    with pytest.raises(ValueError, match="4000 characters"):
        MemoryIn(content="Valid text", metadata={"label": "界" * 700})


@pytest.mark.integration
@pytest.mark.parametrize("metadata", INVALID_METADATA)
async def test_invalid_memory_replacement_preserves_readable_original(boundary_app, metadata):
    path = "/memory/default/retained"
    original = {"content": "保留原始內容 🧠", "metadata": {"標籤": ["測試", {"café": "e\u0301"}]}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary_app, raise_app_exceptions=False),
                                 base_url="http://fixture.test", headers={
                                     "Authorization": "Bearer fixture-requester", "Content-Type": "application/json",
                                 }) as client:
        created = await client.put(path, json=original)
        assert created.status_code == 200, created.text
        before = created.json()
        assert before["content"] == original["content"] and before["metadata"] == original["metadata"]
        response = await client.put(path, content=json.dumps({
            "content": "private-rejected-value", "metadata": metadata,
        }).encode("utf-8"))
        assert_validation(response)
        after = await client.get(path)
        assert after.status_code == 200 and after.json() == before
        found = await client.get("/memory/default", params={"q": "保留原始內容"})
        assert found.status_code == 200 and found.json()["records"][0]["metadata"] == original["metadata"]
