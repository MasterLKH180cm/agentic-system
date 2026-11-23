"""Local HTTP contract fixtures; these do not measure real model performance."""
import asyncio
from dataclasses import replace
import json

import pytest

from agentic.config import INKLING_MODEL
from agentic.worker import Worker
from test_features_integration import REQUESTER, factory as factory
from test_model_failures import provider as provider, response_body

pytestmark = pytest.mark.integration


def local_route(model):
    return replace(model.config.routes[0], model=INKLING_MODEL, api_key="", auth_mode="none",
                   output_token_parameter="max_tokens", reasoning_effort=None)


async def test_local_requests_omit_auth_and_bypass_ambient_proxies(provider, monkeypatch):
    model, store, run_id, state = provider
    model.config = replace(model.config, routes=(local_route(model),))
    proxy_requests = []
    handlers = set()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            proxy_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 502 Proxy must not receive local requests\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            handlers.discard(task)

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id) == "Fixture answer"
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)
    assert proxy_requests == []
    call, = state["calls"]
    assert "authorization" not in call["headers"] and "proxy-authorization" not in call["headers"]
    assert call["payload"]["max_tokens"] == 32
    assert not {"max_completion_tokens", "reasoning_effort", "response_format"} & call["payload"].keys()
    usage, = store.usage("tenant-a", run_id)["calls"]
    assert usage["status"] == "reported" and usage["charged_tokens"] == 10


async def test_local_and_authenticated_fallback_keep_separate_auth_contracts(provider):
    model, store, run_id, state = provider
    model.config = replace(model.config, routes=(local_route(model), model.config.routes[1]))
    state["replies"]["primary"]["status"] = 503
    assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id) == "Fixture answer"
    first, second = state["calls"]
    assert "authorization" not in first["headers"] and "x-api-key" not in first["headers"]
    assert second["headers"]["x-api-key"] == "local-fixture-key"
    assert [call["status"] for call in store.usage("tenant-a", run_id)["calls"]] == [
        "uncertain_http_error", "reported",
    ]


async def test_local_json_planner_and_synthesis_complete_actual_worker_transitions(provider, factory):
    model, _, _, state = provider
    client, app, _ = factory(provider="routed", routes=(local_route(model),))
    plan = {"steps": [
        {"id": "calculate", "tool": "math.calculate", "args": {"expression": "6*7"}},
        {"id": "answer", "tool": "model.summarize", "depends_on": ["calculate"]},
    ]}
    state["replies"]["primary"]["body"] = response_body(text=json.dumps(plan))
    response = client.post("/workflows", headers=REQUESTER, json={"goal": "Calculate 6 times 7"})
    assert response.status_code == 202
    run_id = response.json()["id"]
    worker = Worker(app.state.store, app.state.tools)
    assert await worker.once() == 1
    assert len(app.state.store.get("tenant-a", run_id)["steps"]) == 3
    state["replies"]["primary"]["body"] = response_body(text=json.dumps({
        "outcome": "success", "answer": "42", "cited_ids": [], "refusal_reason": None,
    }))
    assert await worker.once() == 1
    assert await worker.once() == 1
    run = app.state.store.get("tenant-a", run_id)
    assert run["status"] == "completed"
    assert run["steps"][-1]["result"]["answer"] == "42"
    assert len(state["calls"]) == 2
    assert all("authorization" not in call["headers"] for call in state["calls"])
    assert all("max_tokens" in call["payload"] and "tools" not in call["payload"] for call in state["calls"])
    assert app.state.store.usage("tenant-a", run_id)["charged_tokens"] == 20


@pytest.mark.parametrize("fault", ["truncated", "reasoning-only", "tool-calls"])
async def test_local_response_failures_keep_text_contract_and_reported_usage(provider, fault):
    model, store, run_id, state = provider
    model.config = replace(model.config, routes=(local_route(model),))
    body = json.loads(response_body(text='{"steps":[]}'))
    choice = body["choices"][0]
    if fault == "reasoning-only":
        choice["message"].update(content=None, reasoning_content="Private fixture reasoning")
        choice["finish_reason"] = "stop"
    else:
        choice["finish_reason"] = "length" if fault == "truncated" else "tool_calls"
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id)
    call, = store.usage("tenant-a", run_id)["calls"]
    assert call["status"] == "reported" and call["charged_tokens"] == 10
    assert len(state["calls"]) == 1
