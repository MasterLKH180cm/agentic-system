"""Real local TCP provider failures and durable accounting, without model calls."""
import asyncio
import gzip
import json
import time
import threading
from dataclasses import replace

import httpx
import pytest

from agentic.config import ModelRoute, Settings
from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn
from agentic.store import Store
from agentic.async_io import run_sync

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
            if reply.get("header_delay"):
                await asyncio.sleep(reply["header_delay"])
            writer.write(f"HTTP/1.1 {status} Fixture\r\n{encoded_headers}\r\n".encode())
            if reply.get("dribble_seconds"):
                for offset in range(len(body)):
                    writer.write(body[offset:offset + 1])
                    await writer.drain()
                    await asyncio.sleep(reply["dribble_seconds"])
            else:
                writer.write(body)
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


async def settled_usage(store, run_id):
    # After cancellation, the caller's deadline wins over accounting lock waits.
    # The reservation remains charged until bounded background settlement ends.
    async with asyncio.timeout(2):
        while True:
            usage = await run_sync(store.usage, "tenant-a", run_id)
            if all(call["status"] != "reserved" for call in usage["calls"]):
                return usage
            await asyncio.sleep(0.01)


async def test_inkling_transport_preserves_budget_effort_and_separate_reasoning(provider):
    model, store, run_id, state = provider
    route = replace(model.config.routes[0], model="thinkingmachines/Inkling-2-Mini", reasoning_effort=0.2)
    model.config = replace(model.config, routes=(route,))
    body = json.loads(state["replies"]["primary"]["body"])
    body["choices"][0]["message"]["reasoning_content"] = "Private reasoning is never used as answer text."
    body["choices"][0]["finish_reason"] = "stop"
    body["usage"]["prompt_tokens_details"] = {"cached_tokens": 5}
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    assert await complete(model, run_id) == "Fixture answer"
    payload = state["calls"][0]["payload"]
    assert payload["model"] == route.model and payload["reasoning_effort"] == 0.2
    assert payload["max_completion_tokens"] == 32
    assert "response_format" not in payload and "max_tokens" not in payload
    assert store.usage("tenant-a", run_id)["charged_tokens"] == 10


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_truncated_output_is_rejected_even_when_content_is_valid_json(provider, kind):
    model, store, run_id, state = provider
    route = next(route for route in model.config.routes if route.provider == kind)
    model.config = replace(model.config, routes=(route,))
    body = json.loads(response_body(kind, text='{"steps": []}'))
    if kind == "openai":
        body["choices"][0]["finish_reason"] = "length"
    else:
        body["stop_reason"] = "max_tokens"
    state["replies"][route.name]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError, match="truncated"):
        await complete(model, run_id)
    call, = store.usage("tenant-a", run_id)["calls"]
    assert call["status"] == "reported" and call["charged_tokens"] == 10
    assert len(state["calls"]) == 1


async def test_reasoning_only_response_is_not_treated_as_an_answer(provider):
    model, store, run_id, state = provider
    body = json.loads(response_body(text=None))
    body["choices"][0]["message"]["reasoning_content"] = "reasoning only"
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError, match="no text"):
        await complete(model, run_id)
    assert store.usage("tenant-a", run_id)["calls"][0]["status"] == "reported"


@pytest.mark.parametrize(("kind", "reason"), [
    ("openai", "content_filter"), ("openai", "tool_calls"), ("openai", "function_call"),
    ("openai", "unknown"), ("openai", None), ("anthropic", "tool_use"),
    ("anthropic", "refusal"), ("anthropic", "pause_turn"), ("anthropic", None),
])
async def test_nontext_terminal_reasons_cannot_turn_valid_looking_text_into_success(provider, kind, reason):
    model, store, run_id, state = provider
    route = next(route for route in model.config.routes if route.provider == kind)
    model.config = replace(model.config, routes=(route,))
    body = json.loads(response_body(kind, text='{"steps": []}'))
    if kind == "openai":
        body["choices"][0]["finish_reason"] = reason
    else:
        body["stop_reason"] = reason
    state["replies"][route.name]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError, match="finish a text response"):
        await complete(model, run_id)
    call, = store.usage("tenant-a", run_id)["calls"]
    assert call["status"] == "reported" and call["charged_tokens"] == 10
    assert len(state["calls"]) == 1


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
    usage = await settled_usage(store, run_id)
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
    usage = await settled_usage(store, run_id)
    assert len(state["calls"]) == len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "uncertain_cancelled"
    assert usage["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert usage["known_cost_usd"] > 0
    assert not usage["usage_complete"]
    settled = [event for event in store.events("tenant-a", run_id) if event["type"] == "model_usage"]
    assert len(settled) == 1 and settled[0]["payload"]["status"] == "uncertain_cancelled"


@pytest.mark.parametrize("fault", ["stalled-headers", "slow-dribble"])
async def test_total_model_deadline_stops_headers_and_dribbling_without_fallback(provider, fault):
    model, store, run_id, state = provider
    model.config = replace(model.config, model_timeout_seconds=0.25)
    if fault == "stalled-headers":
        state["hold"] = True
    else:
        state["replies"]["primary"]["dribble_seconds"] = 0.02
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(complete(model, run_id), timeout=2)
    assert len(state["calls"]) == 1
    record, = (await settled_usage(store, run_id))["calls"]
    assert record["status"] == "uncertain_timeout"
    assert record["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert record["cost_microusd"] > 0


async def test_fallback_attempts_share_one_elapsed_model_deadline(provider):
    model, store, run_id, state = provider
    model.config = replace(model.config, model_timeout_seconds=0.4)
    state["replies"]["primary"].update(status=503, header_delay=0.25)
    state["replies"]["secondary"]["dribble_seconds"] = 0.02
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(complete(model, run_id), timeout=2)
    assert time.monotonic() - started < 0.6
    assert len(state["calls"]) == 2
    calls = (await settled_usage(store, run_id))["calls"]
    assert [call["status"] for call in calls] == ["uncertain_http_error", "uncertain_timeout"]


async def test_blocking_settlement_cannot_return_late_success_or_erase_reported_usage(provider, monkeypatch):
    model, store, run_id, _ = provider
    model.config = replace(model.config, model_timeout_seconds=0.25)
    settle = store.settle_usage
    settled = threading.Event()

    def delayed_settlement(reservation, **kwargs):
        if kwargs.get("status") == "reported":
            time.sleep(0.3)
        result = settle(reservation, **kwargs)
        settled.set()
        return result

    monkeypatch.setattr(store, "settle_usage", delayed_settlement)
    with pytest.raises(TimeoutError):
        await complete(model, run_id)
    # The deadline returns while the already started database operation is
    # pending. Its reservation remains charged; no competing uncertain write
    # can steal the final reported settlement.
    pending, = store.usage("tenant-a", run_id)["calls"]
    assert pending["status"] in {"reserved", "reported"}
    if pending["status"] == "reserved":
        assert pending["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    assert await asyncio.to_thread(settled.wait, 2)
    record, = store.usage("tenant-a", run_id)["calls"]
    assert record["status"] == "reported" and record["charged_tokens"] == 10


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
