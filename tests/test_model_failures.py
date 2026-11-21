"""Real local TCP provider failures and durable accounting, without model calls."""
import asyncio
import gzip
import json
from dataclasses import replace

import httpx
import pytest

from agentic.config import ModelRoute, Settings
from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn
from agentic.store import Store

pytestmark = pytest.mark.integration


def response_body(provider="openai", text="Fixture answer", usage=None):
    if provider == "openai":
        body = {"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    else:
        body = {"content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 7, "output_tokens": 3}}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body).encode()


@pytest.fixture
async def provider(tmp_path):
    state = {
        "calls": [], "received": asyncio.Event(), "release": asyncio.Event(), "hold": False,
        "replies": {"primary": {"body": response_body()},
                    "secondary": {"body": response_body("anthropic")}},
    }
    handlers = set()
    errors = []

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("ascii").split("\r\n")
            path = lines[0].split()[1]
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            headers = {name.lower(): value for name, value in headers.items()}
            payload = json.loads(await reader.readexactly(int(headers["content-length"])))
            state["calls"].append({"path": path, "headers": headers, "payload": payload})
            state["received"].set()
            if state["hold"]:
                await state["release"].wait()
            reply = state["replies"][path.split("/")[1]]
            if reply.get("disconnect"):
                return
            body = reply["body"]
            response_headers = {
                "Content-Type": "application/json", "Content-Length": str(len(body)),
                "Connection": "close", **reply.get("headers", {}),
            }
            status = reply.get("status", 200)
            encoded_headers = "".join(f"{name}: {value}\r\n" for name, value in response_headers.items())
            writer.write(f"HTTP/1.1 {status} Fixture\r\n{encoded_headers}\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass  # A cancelled client is allowed to close before the fixture replies.
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    routes = tuple(ModelRoute(name, kind, "fixture", base + "/" + name, "local-fixture-key",
                              input_cost_per_million=1, output_cost_per_million=2)
                   for name, kind in (("primary", "openai"), ("secondary", "anthropic")))
    settings = Settings(
        db_path=str(tmp_path / "usage.db"), keys={"fixture": "tenant-a"},
        rag_url="http://127.0.0.1:1", rag_keys={"tenant-a": "unused"}, provider="routed",
        openai_key="", openai_base_url="https://example.invalid", openai_model="fixture",
        anthropic_key="", anthropic_model="fixture", max_parallel=1,
        max_model_output_tokens=32, routes=routes,
    )
    store = Store(settings.db_path)
    run_id = store.submit("tenant-a", WorkflowIn(goal="Calculate a total", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "2+3"})
    ]))["id"]
    try:
        yield ModelGateway(settings, store), store, run_id, state
    finally:
        state["release"].set()
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)
        assert not errors, errors


async def complete(model, run_id):
    return await model.complete("System", "Question", tenant="tenant-a", run_id=run_id)


@pytest.mark.parametrize("fault", ["invalid-json", "compressed", "oversized"])
async def test_protocol_errors_do_not_fallback_or_erase_reserved_charges(provider, fault):
    model, store, run_id, state = provider
    reply = state["replies"]["primary"]
    if fault == "invalid-json":
        reply["body"] = b"not-json"
    elif fault == "compressed":
        reply.update(body=gzip.compress(reply["body"]), headers={"Content-Encoding": "gzip"})
    else:
        reply["body"] = b"x" * 1_000_001
    with pytest.raises(ValueError):
        await complete(model, run_id)
    assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions"]
    assert state["calls"][0]["headers"]["accept-encoding"] == "identity"
    usage = store.usage("tenant-a", run_id)
    assert len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "uncertain_response_error"
    assert usage["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert usage["known_cost_usd"] > 0 and not usage["usage_complete"]


async def test_http_cancellation_retains_uncertain_reservation_without_fallback(provider):
    model, store, run_id, state = provider
    state["hold"] = True
    task = asyncio.create_task(complete(model, run_id))
    try:
        await asyncio.wait_for(state["received"].wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        state["release"].set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    usage = store.usage("tenant-a", run_id)
    assert len(state["calls"]) == len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "uncertain_cancelled"
    assert usage["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert usage["known_cost_usd"] > 0
    assert not usage["usage_complete"]
    settled = [event for event in store.events("tenant-a", run_id) if event["type"] == "model_usage"]
    assert len(settled) == 1 and settled[0]["payload"]["status"] == "uncertain_cancelled"


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_malformed_usage_never_replaces_reservation_with_zero(provider, kind):
    model, store, run_id, state = provider
    usage = ({"prompt_tokens": True, "completion_tokens": 3} if kind == "openai" else
             {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": -1})
    model.config = replace(model.config, routes=(replace(model.config.routes[0], provider=kind),))
    state["replies"]["primary"]["body"] = response_body(kind, usage=usage)
    assert await complete(model, run_id) == "Fixture answer"
    record, = store.usage("tenant-a", run_id)["calls"]
    assert record["status"] == "estimated_missing_usage"
    assert record["input_tokens"] is None and record["output_tokens"] is None
    assert record["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert record["cost_microusd"] > 0


async def test_anthropic_cache_tokens_are_included_in_actual_charges(provider):
    model, store, run_id, state = provider
    model.config = replace(model.config, routes=(replace(model.config.routes[0], provider="anthropic"),))
    state["replies"]["primary"]["body"] = response_body("anthropic", usage={
        "input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 7,
    })
    assert await complete(model, run_id) == "Fixture answer"
    record, = store.usage("tenant-a", run_id)["calls"]
    assert (record["input_tokens"], record["output_tokens"], record["charged_tokens"]) == (14, 3, 17)
    assert record["cost_microusd"] == 20 and record["status"] == "reported"
    assert state["calls"][0]["path"] == "/primary/messages"
    assert state["calls"][0]["payload"]["max_tokens"] == 32


@pytest.mark.parametrize("text,output_tokens,reason", [
    ("", 3, "no text"),
    ("x" * 32001, 3, "response limit"),
    ("Fixture answer", 33, "output token limit"),
], ids=["empty-output", "oversized-text", "over-token-cap"])
async def test_invalid_output_preserves_reported_bill_and_does_not_fallback(provider, text, output_tokens, reason):
    model, store, run_id, state = provider
    state["replies"]["primary"]["body"] = response_body(text=text, usage={
        "prompt_tokens": 7, "completion_tokens": output_tokens,
    })
    with pytest.raises(ValueError, match=reason):
        await complete(model, run_id)
    assert len(state["calls"]) == 1
    record, = store.usage("tenant-a", run_id)["calls"]
    assert record["status"] == "reported"
    assert record["charged_tokens"] == 7 + output_tokens
    assert record["cost_microusd"] == 7 + 2 * output_tokens
    assert sum(event["type"] == "model_usage" for event in store.events("tenant-a", run_id)) == 1


async def test_actual_connection_drop_falls_back_with_original_uncertain_charge(provider):
    model, store, run_id, state = provider
    state["replies"]["primary"]["disconnect"] = True
    assert await complete(model, run_id) == "Fixture answer"
    assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions", "/secondary/messages"]
    calls = store.usage("tenant-a", run_id)["calls"]
    assert [call["status"] for call in calls] == ["uncertain_transport_error", "reported"]
    assert calls[0]["charged_tokens"] > 32 and calls[1]["charged_tokens"] == 10


async def test_exhausted_transient_routes_raise_without_losing_any_reservation(provider):
    model, store, run_id, state = provider
    state["replies"]["primary"]["status"] = 429
    state["replies"]["secondary"]["status"] = 503
    with pytest.raises(httpx.HTTPStatusError) as error:
        await complete(model, run_id)
    assert error.value.response.status_code == 503
    calls = store.usage("tenant-a", run_id)["calls"]
    assert len(state["calls"]) == len(calls) == 2
    assert all(call["status"] == "uncertain_http_error" for call in calls)
    assert all(call["charged_tokens"] > 32 for call in calls)


async def test_provider_redirect_is_not_followed_and_cannot_trigger_fallback(provider):
    model, store, run_id, state = provider
    state["replies"]["primary"].update(status=307, headers={"Location": model.config.routes[1].base_url + "/messages"})
    with pytest.raises(httpx.HTTPStatusError) as error:
        await complete(model, run_id)
    assert error.value.response.status_code == 307
    assert len(state["calls"]) == 1
    assert store.usage("tenant-a", run_id)["calls"][0]["status"] == "uncertain_http_error"


async def test_invalid_request_configuration_is_rejected_before_network_or_reservation(provider):
    model, store, run_id, state = provider
    with pytest.raises(ValueError, match="input exceeds"):
        await model.complete("x" * 100_001, "", tenant="tenant-a", run_id=run_id)
    with pytest.raises(ValueError, match="No model route"):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, purpose="unregistered")
    model.config = replace(model.config, routes=(replace(model.config.routes[0], api_key=""),))
    with pytest.raises(ValueError, match="API key"):
        await complete(model, run_id)
    assert state["calls"] == []
    assert store.usage("tenant-a", run_id)["calls"] == []
