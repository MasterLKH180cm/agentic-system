"""Real local HTTP proves schema transport, output rejection and durable usage."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agentic import model as model_module
from agentic.features import Features
from agentic.model import ModelGateway, response_format
from agentic.store import BudgetExceeded
from test_model_failures import provider as provider, response_body

pytestmark = pytest.mark.integration

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
          "properties": {"answer": {"type": "string", "const": "ok"}}}


def enable(model):
    model.config = replace(model.config, routes=(replace(model.config.routes[0], supports_json_schema=True),))


async def test_schema_http_payload_reservation_and_provider_usage_are_consistent(provider):
    model, store, run_id, state = provider
    enable(model)
    schema = deepcopy(SCHEMA)
    state["hold"] = True
    state["replies"]["primary"]["body"] = response_body(text='{"answer":"ok"}')
    before = model.input_capacity("System", tenant="tenant-a", run_id=run_id)
    after = model.input_capacity("System", tenant="tenant-a", run_id=run_id, response_schema=schema)
    delta = ModelGateway.input_units("System", "", response_schema=schema) - ModelGateway.input_units("System", "")
    assert before - after == delta
    task = asyncio.create_task(model.complete("System", "Question", tenant="tenant-a", run_id=run_id,
                                              purpose="planning", response_schema=schema))
    await asyncio.wait_for(state["received"].wait(), 2)
    pending = store.usage("tenant-a", run_id)
    assert pending["charged_tokens"] == ModelGateway.input_units("System", "Question", response_schema=SCHEMA) + 32
    schema["properties"]["answer"]["const"] = "changed-after-request"
    state["release"].set()
    assert await task == '{"answer":"ok"}'
    request, = state["calls"]
    assert request["payload"]["response_format"] == response_format(SCHEMA)
    assert request["payload"]["max_completion_tokens"] == 32
    assert request["headers"]["content-type"] == "application/json"
    usage = store.usage("tenant-a", run_id)
    assert usage["charged_tokens"] == 10 and usage["calls"][0]["status"] == "reported"
    assert usage["calls"][0]["purpose"] == "planning"


async def test_declared_capability_does_not_change_unstructured_requests(provider):
    model, store, run_id, state = provider
    enable(model)
    assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id) == "Fixture answer"
    assert "response_format" not in state["calls"][0]["payload"]
    assert store.usage("tenant-a", run_id)["charged_tokens"] == 10


@pytest.mark.parametrize("text", [
    "private-response", '```json\n{"answer":"ok"}\n```', '{"answer":"wrong"}',
    '{"answer":"ok","extra":1}', '{"answer":"wrong","answer":"ok"}',
    '{"answer":NaN}', '{"answer":1e309}', '{"answer":"ok"} trailing', 'null',
])
async def test_invalid_structured_output_keeps_actual_reported_usage(provider, text):
    model, store, run_id, state = provider
    enable(model)
    state["replies"]["primary"]["body"] = response_body(text=text)
    with pytest.raises(ValueError, match="invalid structured output") as error:
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA)
    assert "private-response" not in str(error.value)
    usage = store.usage("tenant-a", run_id)
    assert usage["charged_tokens"] == 10 and usage["calls"][0]["status"] == "reported"
    assert len(state["calls"]) == 1


async def test_schema_request_still_rejects_truncation_and_keeps_usage(provider):
    model, store, run_id, state = provider
    enable(model)
    body = json.loads(response_body(text='{"answer":"ok"}'))
    body["choices"][0]["finish_reason"] = "length"
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError, match="truncated"):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA)
    assert store.usage("tenant-a", run_id)["charged_tokens"] == 10


@pytest.mark.parametrize("refusal", ["private provider refusal", " "])
@pytest.mark.parametrize("content", ['{"answer":"ok"}', None])
async def test_refusal_is_rejected_without_fallback_or_usage_loss_even_with_valid_content(provider, refusal, content):
    model, store, run_id, state = provider
    model.config = replace(model.config, routes=tuple(
        replace(route, provider="openai", supports_json_schema=True) for route in model.config.routes))
    body = json.loads(response_body(text=content))
    body["choices"][0].update(finish_reason="stop")
    body["choices"][0]["message"]["refusal"] = refusal
    state["replies"]["primary"]["body"] = json.dumps(body).encode()
    with pytest.raises(ValueError, match="Model refused the requested response") as error:
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA)
    assert str(error.value) == "Model refused the requested response"
    assert len(state["calls"]) == 1
    usage = store.usage("tenant-a", run_id)
    assert usage["charged_tokens"] == 10 and len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "reported"


async def test_schema_budget_denial_occurs_before_http_request(provider):
    model, store, run_id, state = provider
    enable(model)
    model.config = replace(model.config, max_model_tokens=ModelGateway.input_units("System", "Question") + 32)
    with pytest.raises(BudgetExceeded):
        await model.complete("System", "Question", tenant="tenant-a", response_schema=SCHEMA)
    assert state["calls"] == [] and store.usage("tenant-a", run_id)["calls"] == []


async def test_serialized_schema_body_is_bounded_before_reservation(provider, monkeypatch):
    model, store, run_id, state = provider
    enable(model)
    original_size = len(model._request_body(model.config.routes[0], "System", "Question", 32, None))
    monkeypatch.setattr(model_module, "MAX_REQUEST_BYTES", original_size + 1)
    with pytest.raises(ValueError, match="request exceeds"):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA)
    assert state["calls"] == [] and store.usage("tenant-a", run_id)["calls"] == []


@pytest.mark.parametrize("case", ["primary_unsupported", "fallback_unsupported", "invalid_schema", "offline"])
async def test_schema_preflight_fails_without_requests_or_reservations(provider, case):
    model, store, run_id, state = provider
    schema = SCHEMA
    if case != "primary_unsupported":
        primary = replace(model.config.routes[0], supports_json_schema=True)
        model.config = replace(model.config, routes=(primary, model.config.routes[1]) if case == "fallback_unsupported" else (primary,))
    if case == "invalid_schema":
        schema = {"type": "object", "$ref": "https://private.invalid/schema"}
    if case == "offline":
        model.config = replace(model.config, provider="offline")
    with pytest.raises(ValueError):
        await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=schema)
    assert state["calls"] == [] and store.usage("tenant-a", run_id)["calls"] == []


async def test_disabled_fallback_does_not_require_an_unused_routes_capability(provider):
    model, store, run_id, state = provider
    primary = replace(model.config.routes[0], supports_json_schema=True)
    model.config = replace(model.config, routes=(primary, model.config.routes[1]), features=Features({"model_fallback": False}))
    state["replies"]["primary"]["body"] = response_body(text='{"answer":"ok"}')
    assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA) == '{"answer":"ok"}'
    assert len(state["calls"]) == 1 and store.usage("tenant-a", run_id)["charged_tokens"] == 10


async def test_supported_fallback_preserves_schema_and_independent_attempt_accounting(provider):
    model, store, run_id, state = provider
    routes = tuple(replace(route, provider="openai", supports_json_schema=True) for route in model.config.routes)
    model.config = replace(model.config, routes=routes)
    state["replies"]["primary"]["status"] = 503
    state["replies"]["secondary"]["body"] = response_body(text='{"answer":"ok"}')
    assert await model.complete("System", "Question", tenant="tenant-a", run_id=run_id, response_schema=SCHEMA) == '{"answer":"ok"}'
    assert [call["path"] for call in state["calls"]] == ["/primary/chat/completions", "/secondary/chat/completions"]
    assert all(call["payload"]["response_format"] == response_format(SCHEMA) for call in state["calls"])
    usage = store.usage("tenant-a", run_id)
    assert [call["status"] for call in usage["calls"]] == ["uncertain_http_error", "reported"]
    assert usage["charged_tokens"] == ModelGateway.input_units("System", "Question", response_schema=SCHEMA) + 32 + 10
