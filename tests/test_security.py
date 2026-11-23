import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from agentic.api import create_app
from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.planner import plan
from agentic.tools import Tools
from agentic.worker import Worker


def test_distinct_rag_credentials_per_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "multi-tenant.db"))
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"user-a":"a","user-b":"b"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"rag-token-a","b":"rag-token-b"}')
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"approver-a":"a","approver-b":"b"}')
    settings = Settings.from_env()
    recorded = []
    actual_client = httpx.AsyncClient

    def stub_client(*args, **kwargs):
        def handler(request):
            recorded.append(request.headers["authorization"])
            return httpx.Response(200, json={
                "answer": "Verified", "citations": [], "mode_used": "hybrid"
            })
        return actual_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", stub_client)
    app = create_app(settings)
    with TestClient(app) as client:
        for key in ["user-a", "user-b"]:
            response = client.post(
                "/workflows", headers={"Authorization": "Bearer " + key},
                json={"goal": "Find the tenant policy",
                      "steps": [{"id": "search", "tool": "rag.search"}]},
            )
            assert response.status_code == 202
        worker = Worker(app.state.store, Tools(settings, ModelGateway(settings)))
        assert asyncio.run(worker.once()) == 2
    assert sorted(recorded) == ["Bearer rag-token-a", "Bearer rag-token-b"]


def test_reject_shared_rag_token_and_shared_approver_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "security.db"))
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"one":"a","two":"b"}')
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"same","b":"same"}')
    with pytest.raises(ValueError, match="must not share"):
        Settings.from_env()
    monkeypatch.setenv("AGENT_RAG_KEYS_JSON", '{"a":"rag-a","b":"rag-b"}')
    settings = Settings.from_env()
    monkeypatch.setenv("AGENT_APPROVER_KEYS_JSON", '{"one":"a"}')
    with pytest.raises(ValueError, match="disjoint"):
        create_app(settings)


async def test_model_planner_rejects_unallowlisted_tool():
    class Model:
        config = type("Config", (), {"provider": "openai"})()

        async def complete(self, system, goal, *, purpose):
            assert purpose == "planning"
            return '{"steps":[{"id":"danger","tool":"shell.execute","args":{}}]}'

    with pytest.raises(ValueError, match="invalid or unsafe"):
        await plan("Run a dangerous command", Model())
