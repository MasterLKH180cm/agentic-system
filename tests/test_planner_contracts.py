"""Planning schemas guide generation without granting execution authority."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from agentic.config import ModelRoute, Settings
from agentic.features import FeatureDisabled, Features
from agentic.model import ModelGateway
from agentic.planner import MAX_PLANNING_BYTES, plan, plan_response_schema

pytestmark = pytest.mark.unit


def retrieval_plan():
    return {"steps": [
        {"id": "retrieve", "tool": "rag.search", "args": {"question": "Sample retention period", "top_k": 2},
         "depends_on": []},
        {"id": "answer", "tool": "model.summarize", "args": {}, "depends_on": ["retrieve"]},
    ]}


class CapturingModel:
    def __init__(self, response=None, *, features=None, constrained=False):
        self.config = SimpleNamespace(provider="openai", features=Features(features or {}))
        self.response = json.dumps(response if response is not None else retrieval_plan())
        self.calls = []
        self.constrained = constrained

    def routes(self, purpose, snapshot):
        assert purpose == "planning"
        return [SimpleNamespace(supports_json_schema=self.constrained)]

    async def complete(self, system, message, **kwargs):
        self.calls.append({"system": system, "message": message, "kwargs": kwargs})
        return self.response


async def test_planning_advertises_actual_argument_contracts_and_preserves_accounting_scope():
    model = CapturingModel()
    specification = {"allowed_tools": ["rag.search", "model.summarize", "math.calculate"]}
    steps = await plan("Find sample retention and summarize it", model, specification=specification,
                       tenant="tenant-a", run_id="run-a")
    call, = model.calls
    prompt = json.loads(call["message"])
    assert prompt["allowed_tools"] == ["math.calculate", "model.summarize", "rag.search"]
    schema = prompt["response_schema"]
    variants = schema["properties"]["steps"]["items"]["oneOf"]
    assert {variant["properties"]["tool"]["const"] for variant in variants} == set(prompt["allowed_tools"])
    search = schema["$defs"]["rag_search_args"]["anyOf"][1]
    assert "question" in search["properties"] and "query" not in search["properties"]
    assert search["additionalProperties"] is False
    assert search["properties"]["top_k"]["minimum"] == 1
    assert search["properties"]["top_k"]["maximum"] == 10
    assert search["properties"]["row_filters"]["items"]["$ref"] == "#/$defs/rag_search_RowPredicate"
    assert "in" in schema["$defs"]["rag_search_RowPredicate"]["properties"]["op"]["enum"]
    arithmetic = schema["$defs"]["math_calculate_args"]
    assert arithmetic["required"] == ["expression"] and arithmetic["additionalProperties"] is False
    assert call["kwargs"] == {"tenant": "tenant-a", "run_id": "run-a", "purpose": "planning"}
    assert "tool_arguments" not in prompt and "argument_fields" not in prompt
    assert [step.tool for step in steps] == ["rag.search", "model.summarize"]
    assert steps[1].depends_on == ["retrieve"]


async def test_unbound_planning_passes_purpose_even_without_route_introspection():
    model = CapturingModel()
    model.routes = None
    await plan("Find sample retention", model,
               specification={"allowed_tools": ["rag.search", "model.summarize"]})
    assert model.calls[0]["kwargs"] == {"purpose": "planning"}


@pytest.mark.parametrize("constrained", [False, True])
async def test_unbound_planning_uses_and_accounts_for_the_planning_specific_route(
    constrained, tmp_path, monkeypatch,
):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    routes = (
        ModelRoute("synthesis-only", "openai", "synthesis-model", "https://example.invalid/v1", "fixture",
                   purposes=("synthesis",)),
        ModelRoute("planning-only", "openai", "planning-model", "https://example.invalid/v1", "fixture",
                   purposes=("planning",), supports_json_schema=constrained),
    )
    settings = Settings(
        db_path=str(tmp_path / "unbound-planning.db"), keys={"fixture": "local"},
        rag_url="http://127.0.0.1:1", rag_keys={"local": "unused"}, provider="routed",
        openai_key="", openai_base_url="https://example.invalid/v1", openai_model="fixture",
        anthropic_key="", anthropic_model="fixture", max_parallel=1, routes=routes,
    )
    model = ModelGateway(settings)
    requests = []

    async def request(_client, route, body, run_id, _telemetry):
        requests.append((route.name, json.loads(body), run_id))
        return httpx.Response(200, request=httpx.Request("POST", route.base_url + "/chat/completions"), json={
            "choices": [{"message": {"content": json.dumps(retrieval_plan())}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11},
        })

    monkeypatch.setattr(model, "_request", request)
    steps = await plan("Find sample retention", model)
    route_name, body, run_id = requests[0]
    assert len(requests) == 1 and route_name == "planning-only" and run_id is None
    assert body["model"] == "planning-model"
    assert ("response_format" in body) is constrained
    assert [step.tool for step in steps] == ["rag.search", "model.summarize"]
    usage = model.store.usage("local")
    call, = usage["calls"]
    assert (call["route"], call["purpose"], call["tenant"], call["run_id"]) == (
        "planning-only", "planning", "local", None,
    )
    assert usage["usage_complete"] and usage["charged_tokens"] == 18


@pytest.mark.parametrize("allowed,current,snapshot,loop,expected", [
    (["rag.search"], {}, {}, False, ["rag.search"]),
    (["rag.search", "math.calculate"], {"math": False}, {}, False, ["rag.search"]),
    (["rag.search", "math.calculate"], {"math": True}, {"math": False}, False, ["rag.search"]),
    (["rag.search", "math.calculate"], {"math": False}, {"math": True}, False, ["rag.search"]),
    (["rag.search", "artifact.publish"], {"approvals": False}, {}, False, ["rag.search"]),
    (["rag.search", "model.summarize"], {}, {"synthesis": False}, False, ["rag.search"]),
    (["rag.search", "artifact.publish", "agent.run", "workflow.plan"], {}, {}, True, ["rag.search"]),
    (["rag.search", "agent.run", "workflow.plan"], {}, {}, False, ["rag.search"]),
])
async def test_planning_filters_schema_and_name_together(allowed, current, snapshot, loop, expected):
    model = CapturingModel({"steps": [retrieval_plan()["steps"][0]]}, features=current)
    await plan("Find sample retention", model, specification={
        "allowed_tools": allowed, "feature_flags": snapshot,
    }, loop=loop)
    prompt = json.loads(model.calls[0]["message"])
    assert prompt["allowed_tools"] == expected
    variants = prompt["response_schema"]["properties"]["steps"]["items"]["oneOf"]
    assert [variant["properties"]["tool"]["const"] for variant in variants] == expected


@pytest.mark.parametrize("allowed,features,loop", [
    ([], {}, False),
    (["math.calculate"], {"math": False}, False),
    (["artifact.publish"], {}, True),
    (["agent.run", "workflow.plan"], {}, False),
])
async def test_planning_with_no_executable_tool_does_not_call_model(allowed, features, loop):
    model = CapturingModel(features=features)
    with pytest.raises(PermissionError, match="No tools"):
        await plan("Find sample retention", model, specification={"allowed_tools": allowed}, loop=loop)
    assert model.calls == []


async def test_planning_packs_whole_memory_records_as_bounded_utf8_json():
    model = CapturingModel()
    goal = '查詢保留期間，並說明 "引用" 的證據。'
    specification = {"allowed_tools": ["rag.search", "model.summarize"],
                     "instructions": '保留完整指示：換行\n引號 " 與反斜線 \\',
                     "memory_namespace": "bound"}
    feedback = {"reason": '缺少 "保留期間" 的證據。', "score": 0}
    memory = [
        {"id": "first", "content": '完整內容 "one"\\\n'},
        {"id": "oversize", "content": "記" * 9000},
        {"id": "last", "content": "完整的最後一筆。"},
    ]
    original = deepcopy((specification, feedback, memory))
    await plan(goal, model, specification=specification, feedback=feedback, memory=memory)
    message = model.calls[0]["message"]
    prompt = json.loads(message)
    assert len(message.encode("utf-8")) <= MAX_PLANNING_BYTES
    assert prompt["goal"] == goal
    assert prompt["instructions"] == specification["instructions"]
    assert prompt["memory_namespace"] == "bound"
    assert prompt["evaluation_feedback"] == feedback
    assert prompt["memory"] == [memory[0], memory[2]] and prompt["memory_omitted"] == 1
    variants = prompt["response_schema"]["properties"]["steps"]["items"]["oneOf"]
    assert {variant["properties"]["tool"]["const"] for variant in variants} == {"rag.search", "model.summarize"}
    assert (specification, feedback, memory) == original


async def test_oversize_mandatory_context_fails_before_model_without_truncating_instructions():
    model = CapturingModel()
    with pytest.raises(ValueError, match="control context exceeds"):
        await plan("目" * 2000, model, specification={
            "allowed_tools": ["rag.search"], "instructions": "指" * 4000,
        }, feedback={"reason": "不足" * 1000})
    assert model.calls == []


@pytest.mark.parametrize("steps,exception", [
    ([{"id": "search", "tool": "rag.search", "args": {"query": "Sample retention"}}], ValueError),
    ([{"id": "search", "tool": "rag.search", "args": {"question": "Sample retention", "tenant": "other"}}],
     ValueError),
    ([{"id": "search", "tool": "rag.search", "depends_on": ["missing"]}], ValueError),
    ([{"id": "first", "tool": "rag.search", "depends_on": ["second"]},
      {"id": "second", "tool": "model.summarize", "depends_on": ["first"]}], ValueError),
    ([{"id": "publish", "tool": "artifact.publish"}], PermissionError),
    ([{"id": "nested", "tool": "agent.run"}], ValueError),
    ([{"id": f"search{i}", "tool": "rag.search"} for i in range(9)], ValueError),
])
async def test_schema_guidance_does_not_replace_returned_plan_validation(steps, exception):
    model = CapturingModel({"steps": steps})
    with pytest.raises(exception):
        await plan("Find sample retention", model,
                   specification={"allowed_tools": ["rag.search", "model.summarize"]})
    assert len(model.calls) == 1


async def test_returned_disabled_tool_still_hits_execution_feature_validation():
    model = CapturingModel({"steps": [
        {"id": "calculate", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
    ]}, features={"math": False})
    with pytest.raises(FeatureDisabled, match="math"):
        await plan("Calculate 6 times 7", model,
                   specification={"allowed_tools": ["rag.search", "math.calculate"]})
    prompt = json.loads(model.calls[0]["message"])
    assert prompt["allowed_tools"] == ["rag.search"]


async def test_returned_memory_namespace_is_still_bound_by_policy():
    model = CapturingModel({"steps": [
        {"id": "memory", "tool": "memory.search", "args": {"namespace": "private"}},
    ]})
    with pytest.raises(PermissionError, match="bound namespace"):
        await plan("Recall sample retention", model, specification={
            "allowed_tools": ["memory.search"], "allowed_namespaces": ["default", "private"],
            "memory_namespace": "default",
        })


async def test_authorized_publication_still_requires_approval():
    model = CapturingModel({"steps": [{"id": "publish", "tool": "artifact.publish"}]})
    steps = await plan("Publish the authorized result", model,
                       specification={"allowed_tools": ["artifact.publish"]})
    assert json.loads(model.calls[0]["message"])["allowed_tools"] == ["artifact.publish"]
    assert steps[0].approval_required is True


async def test_opted_in_route_gets_schema_once_on_wire_and_keeps_runtime_validation():
    model = CapturingModel(constrained=True)
    await plan("Find sample retention", model,
               specification={"allowed_tools": ["rag.search", "model.summarize"]})
    call, = model.calls
    assert call["kwargs"]["purpose"] == "planning"
    schema = call["kwargs"]["response_schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(model.response))
    prompt = json.loads(call["message"])
    assert "response_schema" not in prompt and "tool_arguments" not in prompt
    assert prompt["argument_fields"]["model.summarize"] == ["question"]
    assert prompt["argument_fields"]["rag.search"] == ["question", "mode", "top_k", "row_filters", "aggregate"]
    assert "tool names" in call["system"] and "requested limit" in call["system"]
    assert "fabricated evidence" in call["system"]


@pytest.mark.parametrize("count", [1, 3, 7, 10])
def test_plan_schema_preserves_general_tool_bounds_without_a_smoke_goal_limit(count):
    schema = plan_response_schema({"rag.search", "model.summarize"})
    Draft202012Validator.check_schema(schema)
    response = retrieval_plan()
    response["steps"][0]["args"]["top_k"] = count
    Draft202012Validator(schema).validate(response)


def test_structured_search_resolves_nested_definitions_at_the_full_response_root():
    schema = plan_response_schema({"rag.search"})
    response = {"steps": [{"id": "rows", "tool": "rag.search", "depends_on": [], "args": {
        "question": "Sum prices of retained rows", "mode": "structured",
        "row_filters": [{"field": "status", "op": "in", "value": ["retained"]}],
        "aggregate": {"operation": "sum", "field": "price"},
    }}]}
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(response)
    assert "rag_search_Aggregation" in schema["$defs"]

    def check(node, *, root=False):
        if isinstance(node, dict):
            assert not {"title", "description", "default"} & node.keys()
            if not root:
                assert "$defs" not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)
    check(schema, root=True)


@pytest.mark.parametrize("fault", [
    "numeric_id", "numeric_string_id", "tool_name_dependency", "hybrid_filters", "hybrid_aggregate",
    "copied_search_args", "fabricated_evidence", "unknown_tool", "unknown_root_field", "unknown_step_field",
    "too_many_steps", "top_k_above_bound",
])
def test_plan_schema_rejects_observed_qwen_contract_failures_and_out_of_contract_outputs(fault):
    schema = plan_response_schema({"rag.search", "model.summarize"})
    response = retrieval_plan()
    search, summary = response["steps"]
    if fault == "numeric_id":
        search["id"] = 1
    elif fault == "numeric_string_id":
        search["id"] = "1"
    elif fault == "tool_name_dependency":
        summary["depends_on"] = ["rag.search"]
    elif fault == "hybrid_filters":
        search["args"].update(mode="hybrid", row_filters=[
            {"field": "sample_retention_period", "op": "contains", "value": "retention"},
        ])
    elif fault == "hybrid_aggregate":
        search["args"].update(mode="hybrid", aggregate={"operation": "count"})
    elif fault == "copied_search_args":
        summary["args"] = deepcopy(search["args"])
    elif fault == "fabricated_evidence":
        summary["args"] = {"evidence": [{"content": "Invented retention period"}]}
    elif fault == "unknown_tool":
        search["tool"] = "math.calculate"
        search["args"] = {"expression": "1+1"}
    elif fault == "unknown_root_field":
        response["answer"] = "Invented result"
    elif fault == "unknown_step_field":
        search["evidence"] = "Invented result"
    elif fault == "too_many_steps":
        response["steps"] = [{**search, "id": f"search{i}"} for i in range(9)]
    else:
        search["args"]["top_k"] = 11
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema).validate(response)


async def test_actual_failed_qwen_plan_is_rejected_without_output_repair():
    arguments = {"question": "What is the sample retention period?", "mode": "hybrid", "top_k": 5,
                 "row_filters": [{"field": "sample_retention_period", "op": "contains", "value": "retention"}],
                 "aggregate": None}
    response = {"steps": [
        {"id": 1, "tool": "rag.search", "args": arguments, "depends_on": []},
        {"id": 2, "tool": "model.summarize", "args": deepcopy(arguments), "depends_on": ["rag.search"]},
    ]}
    model = CapturingModel(response, constrained=True)
    original = model.response
    with pytest.raises(ValueError, match="invalid or unsafe"):
        await plan("Find sample retention", model,
                   specification={"allowed_tools": ["rag.search", "model.summarize"]})
    assert model.response == original and len(model.calls) == 1


@pytest.mark.parametrize("fault", ["unknown_dependency", "duplicate_id", "cycle"])
async def test_locally_enforced_cross_step_rules_remain_required_after_schema_guidance(fault):
    response = retrieval_plan()
    if fault == "unknown_dependency":
        response["steps"][1]["depends_on"] = ["missing"]
    elif fault == "duplicate_id":
        response["steps"][1]["id"] = "retrieve"
        response["steps"][1]["depends_on"] = []
    else:
        response["steps"][0]["depends_on"] = ["answer"]
    schema = plan_response_schema({"rag.search", "model.summarize"})
    Draft202012Validator(schema).validate(response)
    model = CapturingModel(response, constrained=True)
    with pytest.raises(ValueError, match="invalid or unsafe"):
        await plan("Find sample retention", model,
                   specification={"allowed_tools": ["rag.search", "model.summarize"]})


@pytest.mark.parametrize("response", [
    '{"steps":[{"id":"search","tool":"rag.search"}],"answer":"invented"}',
    '{"steps":[],"steps":[{"id":"search","tool":"rag.search"}]}',
])
async def test_root_extras_and_ambiguous_json_are_rejected_without_cleaning(response):
    model = CapturingModel()
    model.response = response
    with pytest.raises(ValueError, match="invalid or unsafe"):
        await plan("Find sample retention", model, specification={"allowed_tools": ["rag.search"]})
    assert model.response == response


@pytest.mark.parametrize("allowed", [set(), {"agent.run"}, {"workflow.plan"}, {"shell.execute"}])
def test_schema_builder_cannot_advertise_internal_or_unregistered_tools(allowed):
    with pytest.raises(ValueError, match="allowed executable tools"):
        plan_response_schema(allowed)
