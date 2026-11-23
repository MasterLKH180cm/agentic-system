"""Durable synthesis wire/rendering fixtures; these do not claim model quality."""
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from agentic.config import ModelRoute, Settings
from agentic.evidence import SOURCE_ID, SYNTHESIS_SYSTEM, collect_evidence, encode_json, validate_synthesis
from agentic.features import Features
from agentic.model import ModelGateway, response_format
from agentic.schemas import Step, WorkflowIn
from agentic.tools import SYNTHESIS_TEXT_SYSTEM, Tools, _render_synthesis_text, _synthesis_response_schema

pytestmark = pytest.mark.integration

SOURCE = {"chunk_id": "c1", "document_id": "archive", "text": "Exports are retained for 14 days."}
GOAL = "What is the export retention period?"


def wire(text="Exports are retained for 14 days.", *, ids=None, outcome="success", reason=None):
    return {"outcome": outcome, "text": text, "cited_ids": ["c1"] if ids is None else ids,
            "refusal_reason": reason}


def success_branch(schema):
    return next(branch for branch in schema["oneOf"] if branch["properties"]["outcome"]["const"] == "success")


@pytest.fixture
def synthesis_system(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        db_path=str(tmp_path / "synthesis.db"), keys={"fixture": "local"},
        rag_url="http://127.0.0.1:1", rag_keys={"local": "unused"}, provider="routed",
        openai_key="", openai_base_url="https://example.invalid/v1", openai_model="fixture",
        anthropic_key="", anthropic_model="fixture", max_parallel=1, max_model_output_tokens=512,
        features=Features({"artifacts": False}), routes=(
            ModelRoute("planning-only", "openai", "planning", "https://example.invalid/v1", "fixture",
                       purposes=("planning",), supports_json_schema=True),
            ModelRoute("synthesis-only", "openai", "synthesis", "https://example.invalid/v1", "fixture",
                       purposes=("synthesis",), supports_json_schema=True),
        ),
    )
    model = ModelGateway(settings)
    tools = Tools(settings, model)
    state = {"text": json.dumps(wire()), "calls": [], "capacity": []}
    original_capacity = model.input_capacity

    def capacity(system, **kwargs):
        value = original_capacity(system, **kwargs)
        state["capacity"].append({"system": system, "kwargs": deepcopy(kwargs), "value": value})
        return value

    async def request(_client, route, body, run_id, _telemetry):
        state["calls"].append({"route": route.name, "payload": json.loads(body), "run_id": run_id,
                               "reserved": model.store.usage("local", run_id)["charged_tokens"]})
        return httpx.Response(200, request=httpx.Request("POST", route.base_url + "/chat/completions"), json={
            "choices": [{"finish_reason": "stop", "message": {"content": state["text"]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 50},
        })

    monkeypatch.setattr(model, "input_capacity", capacity)
    monkeypatch.setattr(model, "_request", request)
    return tools, state


def prepare(tools, *, evidence=None, goal=GOAL, budget=12000, snapshot=None):
    store = tools.store
    run_id = store.submit("local", WorkflowIn(goal=goal, steps=[
        Step(id="read", tool="rag.search"), Step(id="answer", tool="model.summarize", depends_on=["read"]),
    ]), specification={"max_model_tokens": budget, "feature_flags": snapshot or {}})["id"]
    source_job = store.claim()
    assert source_job["id"] == "read" and source_job["run_id"] == run_id
    assert store.complete(source_job, evidence if evidence is not None else {"citations": [SOURCE]})
    job = store.claim()
    assert job["id"] == "answer" and job["run_id"] == run_id
    return job, store.input_results(run_id, job["depends_on"])


@pytest.mark.parametrize("arithmetic", [False, True])
async def test_synthesis_wire_schema_rendering_capacity_routing_and_reported_usage(synthesis_system, arithmetic):
    tools, state = synthesis_system
    evidence = {"expression": "19 * 23", "value": 437} if arithmetic else {"citations": [SOURCE]}
    expected = wire("437", ids=[]) if arithmetic else wire()
    state["text"] = json.dumps(expected)
    job, inputs = prepare(tools, evidence=evidence)
    result = await tools.execute(job, inputs)
    request, = state["calls"]
    assert request["route"] == "synthesis-only" and request["run_id"] == job["run_id"]
    payload = request["payload"]
    schema = payload["response_format"]["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(expected)
    assert "pattern" not in json.dumps(schema)
    for branch in schema["oneOf"]:
        assert branch["additionalProperties"] is False
        assert set(branch["required"]) == {"outcome", "text", "cited_ids", "refusal_reason"}
        assert branch["properties"]["text"]["maxLength"] == 7940
    ids = success_branch(schema)["properties"]["cited_ids"]
    assert ids["uniqueItems"] and ids["maxItems"] == (0 if arithmetic else 1)
    if arithmetic:
        assert "enum" not in ids["items"] and result["citations"] == []
    else:
        assert ids["items"]["enum"] == ["c1"] and ids["minItems"] == 1
        assert result["citations"][0]["chunk_id"] == "c1"
    capacity, = state["capacity"]
    assert capacity["kwargs"] == {"tenant": "local", "run_id": job["run_id"], "response_schema": schema}
    system, prompt = (message["content"] for message in payload["messages"])
    assert system == capacity["system"] == SYNTHESIS_TEXT_SYSTEM
    assert "synthesis-text-v1" in system and len(prompt.encode()) <= capacity["value"]
    assert "response_schema" not in json.loads(prompt)
    assert request["reserved"] == ModelGateway.input_units(system, prompt, response_schema=schema) + 512
    usage = tools.store.usage("local", job["run_id"])
    call, = usage["calls"]
    assert (call["purpose"], call["route"], call["status"]) == ("synthesis", "synthesis-only", "reported")
    assert usage["charged_tokens"] == 170
    assert result["answer"] == expected["text"] + ("" if arithmetic else " [c1]")


@pytest.mark.parametrize("text", [
    'The "archive" retains exports for 14 days.',
    r"Exports at C:\archive\exports remain for 14 days.",
    "Exports remain for 14 days.\nThen they are deleted.\tEnd.",
    "Exports remain for 14 days.\n\"Archive\" at C:\\archive remains unmodified.\n",
    "Exports remain for 14 days [c1].",
])
async def test_renderer_preserves_original_prose_and_always_appends_model_selected_references(synthesis_system, text):
    tools, state = synthesis_system
    state["text"] = json.dumps(wire(text))
    original = state["text"]
    job, inputs = prepare(tools, evidence={"citations": [{**SOURCE, "text": text}]})
    result = await tools.execute(job, inputs)
    assert result["answer"] == text + " [c1]"
    assert result["citations"][0]["chunk_id"] == "c1" and state["text"] == original


async def test_renderer_preserves_model_source_order_and_does_not_select_other_available_sources(synthesis_system):
    tools, state = synthesis_system
    state["text"] = json.dumps(wire(ids=["c2", "c1"]))
    job, inputs = prepare(tools, evidence={"citations": [SOURCE, {**SOURCE, "chunk_id": "c2"},
                                                      {**SOURCE, "chunk_id": "unused"}]})
    result = await tools.execute(job, inputs)
    assert result["answer"] == wire()["text"] + " [c2] [c1]"
    assert {source["chunk_id"] for source in result["citations"]} == {"c1", "c2"}
    assert "unused" not in result["answer"]


async def test_unopted_in_synthesis_keeps_legacy_wire_and_prompt(synthesis_system):
    tools, state = synthesis_system
    tools.model.config = replace(tools.model.config, routes=tuple(
        replace(route, supports_json_schema=False) if route.name == "synthesis-only" else route
        for route in tools.model.config.routes
    ))
    legacy = {"outcome": "success", "answer": "14 days [c1].", "cited_ids": ["c1"], "refusal_reason": None}
    state["text"] = json.dumps(legacy)
    job, inputs = prepare(tools)
    assert (await tools.execute(job, inputs))["answer"] == legacy["answer"]
    payload = state["calls"][0]["payload"]
    assert "response_format" not in payload and payload["messages"][0]["content"] == SYNTHESIS_SYSTEM
    assert "response_schema" not in state["capacity"][0]["kwargs"]


async def test_offline_synthesis_does_not_inspect_routes_or_use_new_contract(synthesis_system, monkeypatch):
    tools, state = synthesis_system
    tools.config = tools.model.config = replace(tools.config, provider="offline")

    def unexpected_routes(*_args):
        pytest.fail("Offline synthesis must not inspect model routes")

    monkeypatch.setattr(tools.model, "routes", unexpected_routes)
    job, inputs = prepare(tools)
    result = await tools.execute(job, inputs)
    assert result["outcome"] == "success" and "14 days" in result["answer"]
    assert not state["calls"] and not tools.store.usage("local", job["run_id"])["calls"]
    assert state["capacity"][0]["system"] == SYNTHESIS_SYSTEM
    assert "response_schema" not in state["capacity"][0]["kwargs"]


@pytest.mark.parametrize("fallback_enabled", [False, True])
async def test_synthesis_respects_snapshot_and_never_drops_schema_for_unsupported_fallback(
    synthesis_system, fallback_enabled,
):
    tools, state = synthesis_system
    fallback = replace(tools.model.config.routes[-1], name="unsupported-fallback", supports_json_schema=False)
    tools.model.config = replace(tools.model.config, routes=(*tools.model.config.routes, fallback))
    job, inputs = prepare(tools, snapshot={"model_fallback": fallback_enabled})
    if fallback_enabled:
        with pytest.raises(ValueError, match="Every selected model route"):
            await tools.execute(job, inputs)
        assert not state["calls"] and not tools.store.usage("local", job["run_id"])["calls"]
    else:
        assert (await tools.execute(job, inputs))["outcome"] == "success"
        assert len(state["calls"]) == 1 and "response_format" in state["calls"][0]["payload"]


@pytest.mark.parametrize("arithmetic_only", [False, True])
async def test_schema_accounts_for_admitted_ids_then_narrows_to_only_packed_sources(
    synthesis_system, arithmetic_only,
):
    tools, state = synthesis_system
    extra = {"chunk_id": "unpacked-" + "x" * 191, "document_id": "other", "text": "Other exports last 14 days."}
    evidence = {"citations": [SOURCE, extra]}
    if arithmetic_only:
        evidence.update(expression="19 * 23", value=437)
        state["text"] = json.dumps(wire("437", ids=[]))
    bundle = collect_evidence({"read": evidence})
    desired = {"question": GOAL, "sources": [] if arithmetic_only else [SOURCE], "observations": bundle.observations}
    schema = _synthesis_response_schema(list(bundle.sources))
    budget = ModelGateway.input_units(SYNTHESIS_TEXT_SYSTEM, encode_json(desired), response_schema=schema) + 512
    job, inputs = prepare(tools, evidence=evidence, budget=budget)
    result = await tools.execute(job, inputs)
    call, = state["calls"]
    payload = call["payload"]
    packed = json.loads(payload["messages"][1]["content"])
    assert [source["chunk_id"] for source in packed["sources"]] == ([] if arithmetic_only else ["c1"])
    initial = state["capacity"][0]["kwargs"]["response_schema"]
    final = payload["response_format"]["json_schema"]["schema"]
    assert set(success_branch(initial)["properties"]["cited_ids"]["items"]["enum"]) == {"c1", extra["chunk_id"]}
    assert success_branch(final)["properties"]["cited_ids"]["maxItems"] == (0 if arithmetic_only else 1)
    assert extra["chunk_id"] not in json.dumps(final)
    assert ModelGateway.input_units("", "", response_schema=final) <= ModelGateway.input_units("", "", response_schema=initial)
    assert call["reserved"] <= budget and result["outcome"] == "success"


@pytest.mark.parametrize("motif", ["x", ".", "-", ":", "..a", "..-", "..:", ".a", ".-", ".:-_a"])
def test_maximum_authorized_ids_keep_bounded_monotonic_schemas_without_patterns(motif):
    ids = [(motif * 200)[:197] + f"{index:03d}" for index in range(20)]
    assert len(set(ids)) == 20 and all(len(identifier) == 200 and SOURCE_ID.fullmatch(identifier) for identifier in ids)
    costs = []
    for count in (20, 10, 1, 0):
        schema = _synthesis_response_schema(ids[:count])
        response_format(schema)
        serialized = encode_json(schema)
        assert len(serialized.encode()) <= 16 * 1024 and "pattern" not in serialized
        costs.append(ModelGateway.input_units("", "", response_schema=schema))
        assert success_branch(schema)["properties"]["cited_ids"]["maxItems"] == count
        Draft202012Validator(schema).validate(wire("Supported", ids=ids[:1] if count else []))
    assert costs == sorted(costs, reverse=True) and len(set(costs)) == 4


def test_maximum_rendered_answer_is_exactly_bounded_without_truncation():
    ids = [f"c{index:02d}-" + "x" * 196 for index in range(20)]
    sources = [{**SOURCE, "chunk_id": identifier} for identifier in ids]
    original = wire("x" * 7940, ids=ids)
    rendered = _render_synthesis_text(original, sources)
    assert len(rendered.answer) == 12000 and rendered.answer.startswith(original["text"])
    assert rendered.cited_ids == ids and len(validate_synthesis(rendered.answer, sources, [])) == 20
    with pytest.raises(ValueError):
        _render_synthesis_text(wire("x" * 7941, ids=ids), sources)


async def test_quarantined_source_ids_are_never_advertised_or_rendered(synthesis_system):
    tools, state = synthesis_system
    excluded = {"chunk_id": "quarantined", "document_id": "private", "text": "Private content."}
    evidence = {"outputs": {"good": {"citations": [SOURCE]},
                             "refused": {"outcome": "insufficient_evidence", "refusal_reason": "model_rejected",
                                         "citations": [excluded]}}}
    job, inputs = prepare(tools, evidence=evidence)
    result = await tools.execute(job, inputs)
    assert result["outcome"] == "success" and "quarantined" not in result["answer"]
    assert "quarantined" not in json.dumps(state["capacity"])
    assert "quarantined" not in json.dumps(state["calls"])


@pytest.mark.parametrize("budget,evidence,reason", [
    (12000, {"citations": []}, "no_candidates"),
    (1400, {"citations": [SOURCE]}, "context_budget_exhausted"),
])
async def test_empty_evidence_or_schema_exhausted_capacity_makes_no_request(
    synthesis_system, budget, evidence, reason,
):
    tools, state = synthesis_system
    job, inputs = prepare(tools, evidence=evidence, budget=budget)
    result = await tools.execute(job, inputs)
    assert result["outcome"] == "insufficient_evidence" and result["refusal_reason"] == reason
    assert not state["calls"] and not tools.store.usage("local", job["run_id"])["calls"]


@pytest.mark.parametrize("value", [
    wire(ids=[]), wire(ids=["foreign"]), wire(ids=["c1", "c1"]), wire(ids=[1]),
    {key: value for key, value in wire().items() if key != "cited_ids"},
    {key: value for key, value in wire().items() if key != "text"},
    {"outcome": "success", "answer": "14 days [c1].", "cited_ids": ["c1"], "refusal_reason": None},
    {**wire(), "answer": "Legacy field must not be accepted"},
    wire(reason="model_rejected"), wire(outcome="insufficient_evidence", reason="model_rejected"),
    wire(ids=[], outcome="insufficient_evidence", reason=None),
])
async def test_invalid_wire_selection_or_shape_never_gets_repaired_and_keeps_usage(synthesis_system, value):
    tools, state = synthesis_system
    state["text"] = json.dumps(value)
    original = state["text"]
    job, inputs = prepare(tools)
    with pytest.raises(ValueError):
        await tools.execute(job, inputs)
    assert len(state["calls"]) == 1 and state["text"] == original
    assert tools.store.usage("local", job["run_id"])["charged_tokens"] == 170
    with pytest.raises(ValueError):
        _render_synthesis_text(value, [SOURCE])


@pytest.mark.parametrize("text,ids,extra_source", [
    ("Exports last 140 days.", ["c1"], None),
    ("Exports last 140 days.", ["c1"], {**SOURCE, "chunk_id": "unused", "text": "Exports last 140 days."}),
    ("Exports last 14 days [foreign].", ["c1"], None),
    ("Exports last 14 days [c2].", ["c1"], {**SOURCE, "chunk_id": "c2"}),
    ("  \n\t", ["c1"], None),
])
async def test_schema_valid_but_ungrounded_or_unselected_inline_text_still_fails(synthesis_system, text, ids, extra_source):
    tools, state = synthesis_system
    value = wire(text, ids=ids)
    state["text"] = json.dumps(value)
    sources = [SOURCE] + ([extra_source] if extra_source else [])
    job, inputs = prepare(tools, evidence={"citations": sources})
    with pytest.raises(ValueError):
        await tools.execute(job, inputs)
    schema = state["calls"][0]["payload"]["response_format"]["json_schema"]["schema"]
    Draft202012Validator(schema).validate(value)
    assert tools.store.usage("local", job["run_id"])["charged_tokens"] == 170
    assert state["text"] == json.dumps(value) and len(state["calls"]) == 1


@pytest.mark.parametrize("source_ids", [[], ["c1"], ["c1", "c2"]])
def test_refusal_schema_never_requires_a_source_reference(source_ids):
    schema = _synthesis_response_schema(source_ids)
    value = wire('Private "refusal"\nC:\\path', ids=[], outcome="insufficient_evidence", reason="model_rejected")
    Draft202012Validator(schema).validate(value)
    assert _render_synthesis_text(value, [{**SOURCE, "chunk_id": identifier} for identifier in source_ids]).answer == value["text"]


async def test_valid_model_refusal_is_preserved_without_exposing_provider_explanation(synthesis_system):
    tools, state = synthesis_system
    state["text"] = json.dumps(wire("Private provider explanation", ids=[],
                                    outcome="insufficient_evidence", reason="model_rejected"))
    job, inputs = prepare(tools)
    result = await tools.execute(job, inputs)
    assert result["outcome"] == "insufficient_evidence" and result["refusal_reason"] == "model_rejected"
    assert result["quarantined_ids"] == ["c1"] and result["citations"] == []
    assert "Private provider explanation" not in json.dumps(result)
    assert tools.store.usage("local", job["run_id"])["charged_tokens"] == 170


async def test_structured_adapter_cannot_return_none_and_silently_fall_back_to_extraction(synthesis_system, monkeypatch):
    tools, state = synthesis_system

    async def no_response(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tools.model, "complete", no_response)
    job, inputs = prepare(tools)
    with pytest.raises(ValueError, match="Structured synthesis returned no output"):
        await tools.execute(job, inputs)
    assert not state["calls"] and not tools.store.usage("local", job["run_id"])["calls"]


@pytest.mark.parametrize("raw", [
    '```json\n{"outcome":"success","text":"14 days","cited_ids":["c1"],"refusal_reason":null}\n```',
    '{"outcome":"success","text":"wrong","text":"14 days","cited_ids":["c1"],"refusal_reason":null}',
])
async def test_invalid_json_is_not_repaired_into_the_new_wire_contract(synthesis_system, raw):
    tools, state = synthesis_system
    state["text"] = raw
    job, inputs = prepare(tools)
    with pytest.raises(ValueError, match="invalid structured output"):
        await tools.execute(job, inputs)
    assert state["text"] == raw and len(state["calls"]) == 1
    assert tools.store.usage("local", job["run_id"])["charged_tokens"] == 170


def test_observation_only_wire_cannot_select_a_fabricated_source():
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(_synthesis_response_schema([])).validate(wire("437", ids=["source_1"]))
    with pytest.raises(ValueError, match="unknown evidence"):
        _render_synthesis_text(wire("437", ids=["source_1"]), [])
