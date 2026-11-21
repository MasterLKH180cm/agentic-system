"""Pure contracts for plans, authorization, configuration and usage accounting."""
import json

import pytest
from pydantic import ValidationError

from agentic.config import ModelRoute, model_routes
from agentic.control import ControlPlane
from agentic.model import ModelGateway, estimated_cost, reported_usage
from agentic.postgres import Row, bind_parameters, translate_schema
from agentic.schemas import (
    AgentDefinition,
    MemoryIn,
    PolicyDefinition,
    SearchArgs,
    Step,
    WorkflowDefinition,
    WorkflowIn,
)

pytestmark = pytest.mark.unit


def calculation(name="calculate", dependencies=()):
    return {"id": name, "tool": "math.calculate", "args": {"expression": "2 + 3"},
            "depends_on": list(dependencies)}


@pytest.mark.parametrize("steps,reason", [
    ([], "at least one step"),
    ([calculation(), calculation()], "Duplicate step ID"),
    ([calculation(dependencies=["missing"])], "unknown step dependency"),
    ([calculation(dependencies=["calculate"])], "Self or unknown"),
    ([calculation("a", ["b"]), calculation("b", ["a"])], "Cyclic workflow"),
    ([calculation("a"), calculation("b", ["a", "a"])], "Duplicate dependencies"),
])
def test_workflow_rejects_invalid_dependency_graphs(steps, reason):
    with pytest.raises(ValidationError, match=reason):
        WorkflowIn(goal="Calculate a total", steps=steps)


def test_valid_unordered_diamond_preserves_dependencies_and_approval():
    workflow = WorkflowIn(goal="Publish the combined calculations", steps=[
        {"id": "publish", "tool": "artifact.publish", "depends_on": ["left", "right"]},
        calculation("left", ["root"]), calculation("right", ["root"]), calculation("root"),
    ])
    assert [step.id for step in workflow.steps] == ["publish", "left", "right", "root"]
    assert workflow.steps[0].approval_required
    assert workflow.steps[0].depends_on == ["left", "right"]


@pytest.mark.parametrize("tool", ["agent.run", "workflow.plan"])
def test_client_and_registry_cannot_inject_internal_runtime_steps(tool):
    step = {"id": "internal", "tool": tool}
    with pytest.raises(ValidationError, match="Internal runtime"):
        WorkflowIn(goal="Run an internal engine step", steps=[step])
    with pytest.raises(ValidationError, match="Nested runtime"):
        WorkflowDefinition(name="unsafe", version=1, steps=[step])


@pytest.mark.parametrize("fields", [
    {"agent_version": 1}, {"policy_version": 1}, {"workflow_version": 1},
    {"runtime_mode": "agent_loop", "steps": [calculation()]},
    {"workflow_id": "saved", "steps": [calculation()]},
    {"workflow_id": "saved", "runtime_mode": "agent_loop"},
])
def test_workflow_definitions_cannot_be_ambiguously_selected(fields):
    with pytest.raises(ValidationError):
        WorkflowIn(goal="Calculate a total", **fields)


@pytest.mark.parametrize("fields", [
    {"mode": "hybrid", "aggregate": {"operation": "count"}},
    {"mode": "structured", "aggregate": {"operation": "sum"}},
    {"mode": "structured", "row_filters": [{"field": "id", "op": "in", "value": "a"}]},
    {"mode": "structured", "row_filters": [{"field": "id", "op": "in", "value": [{"id": "a"}]}]},
    {"mode": "structured", "row_filters": [{"field": "id", "op": "in", "value": list(range(101))}]},
    {"mode": "structured", "row_filters": [{"field": "price", "op": "gt", "value": float("inf")}]},
    {"url": "https://caller-selected.invalid"},
    {"threshold": 0.1},
])
def test_retrieval_contract_rejects_invalid_or_caller_controlled_policy(fields):
    with pytest.raises(ValidationError):
        SearchArgs(**fields)


def test_structured_contract_preserves_zero_false_and_empty_membership():
    args = SearchArgs(mode="structured", row_filters=[
        {"field": "amount", "op": "eq", "value": 0},
        {"field": "active", "op": "eq", "value": False},
        {"field": "id", "op": "in", "value": []},
    ], aggregate={"operation": "count"})
    assert [predicate.value for predicate in args.row_filters] == [0, False, []]
    assert type(args.row_filters[1].value) is bool
    assert args.aggregate.field is None


@pytest.mark.parametrize("step,specification,loop", [
    ({"id": "publish", "tool": "artifact.publish"}, {"allowed_tools": ["math.calculate"]}, False),
    ({"id": "plan", "tool": "workflow.plan"}, {"allowed_tools": ["workflow.plan"]}, False),
    ({"id": "memory", "tool": "memory.search", "args": {"namespace": "private"}},
     {"allowed_namespaces": ["default"]}, False),
    ({"id": "memory", "tool": "memory.search", "args": {"namespace": "private"}},
     {"allowed_namespaces": ["default", "private"], "memory_namespace": "default"}, False),
    ({**calculation(), "approval_required": True}, {}, True),
    (calculation(), {"max_steps": 0}, False),
])
def test_compiled_plan_policy_cannot_expand_tool_or_namespace_permissions(step, specification, loop):
    with pytest.raises(PermissionError):
        ControlPlane.validate_steps([Step.model_validate(step)], specification, loop=loop)


def test_registry_and_metadata_contracts_reject_privilege_or_size_expansion():
    for tool in ("artifact.publish", "agent.run", "workflow.plan"):
        with pytest.raises(ValidationError, match="read-only"):
            AgentDefinition(name="unsafe", version=1, allowed_tools=[tool])
    with pytest.raises(ValidationError, match="Internal planning"):
        PolicyDefinition(name="unsafe", version=1, allowed_tools=["workflow.plan"])
    with pytest.raises(ValidationError, match="namespace"):
        PolicyDefinition(name="unsafe", version=1, allowed_namespaces=["../private"])
    with pytest.raises(ValidationError, match="8000"):
        Step(id="oversize", tool="rag.search", args={"question": "a" * 8000})
    with pytest.raises(ValidationError, match="4000"):
        MemoryIn(content="bounded memory", metadata={"padding": "a" * 4000})


@pytest.mark.parametrize("price", [True, False, -1, 1_000_001, 10**400, float("nan"), float("inf"), "1.0"])
def test_route_prices_reject_non_numeric_nonfinite_and_out_of_range_values(monkeypatch, price):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([
        {"name": "test", "provider": "openai", "input_cost_per_million": price}
    ]))
    with pytest.raises(ValueError, match="prices"):
        model_routes("openai", "fixture-key", "")


def test_zero_and_fractional_prices_keep_microdollar_rounding(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([
        {"name": "test", "provider": "openai", "input_cost_per_million": 0,
         "output_cost_per_million": 0.25}
    ]))
    route, = model_routes("openai", "fixture-key", "")
    assert route.input_cost_per_million == 0
    assert estimated_cost(route, 10_000, 3) == 1
    assert estimated_cost(route, 10_000, 4) == 1
    assert estimated_cost(ModelRoute("unpriced", "openai", "fixture", "https://example.invalid", "key"), 1, 1) is None


@pytest.mark.parametrize("entry,reason", [
    ({"unexpected": 1}, "fields"),
    ({"provider": "unregistered"}, "provider"),
    ({"base_url": "https://user:secret@example.invalid"}, "endpoint"),
    ({"purposes": ["unregistered"]}, "purposes"),
    ({"name": ""}, "name"),
])
def test_route_configuration_fails_closed(monkeypatch, entry, reason):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([entry]))
    with pytest.raises(ValueError, match=reason):
        model_routes("openai", "fixture-key", "")


def test_route_list_bounds_credentials_and_names_are_enforced(monkeypatch):
    for value in ({}, [], [{}] * 9):
        monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps(value))
        with pytest.raises(ValueError, match="1..8 routes"):
            model_routes("openai", "fixture-key", "")
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", '[{"name":"same"},{"name":"same"}]')
    with pytest.raises(ValueError, match="unique"):
        model_routes("openai", "fixture-key", "")
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", '[{}]')
    with pytest.raises(ValueError, match="API key"):
        model_routes("openai", "", "")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON")
    route, = model_routes("anthropic", "", "fixture-key")
    assert route.provider == "anthropic" and route.name == "default"
    assert model_routes("offline", "", "") == ()


@pytest.mark.parametrize("provider,usage,expected", [
    ("openai", {"prompt_tokens": 0, "completion_tokens": 0}, (0, 0)),
    ("openai", {"prompt_tokens": 2_000_000_000, "completion_tokens": 1}, (2_000_000_000, 1)),
    ("anthropic", {"input_tokens": 2, "output_tokens": 3}, (2, 3)),
    ("anthropic", {"input_tokens": 2, "output_tokens": 3,
                   "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}, (14, 3)),
    ("openai", None, None),
    ("openai", [], None),
    ("openai", {"prompt_tokens": 2}, None),
    ("openai", {"prompt_tokens": True, "completion_tokens": 3}, None),
    ("openai", {"prompt_tokens": -1, "completion_tokens": 3}, None),
    ("openai", {"prompt_tokens": 2.0, "completion_tokens": 3}, None),
    ("openai", {"prompt_tokens": 2, "completion_tokens": 2_000_000_001}, None),
    ("anthropic", {"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": False}, None),
    ("anthropic", {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": -1}, None),
])
def test_provider_usage_requires_complete_valid_counters(provider, usage, expected):
    assert reported_usage(provider, usage) == expected


def test_input_accounting_includes_utf8_and_message_framing():
    assert ModelGateway.input_units("", "") == 128
    assert ModelGateway.input_units("system", "中文") == 140


def test_postgres_translation_preserves_literals_and_deadline_precision():
    query = "SELECT '?', 'it''s ?' FROM runs WHERE tenant=? AND goal=? AND status='queued'"
    assert bind_parameters(query) == (
        "SELECT '?', 'it''s ?' FROM runs WHERE tenant=%s AND goal=%s AND status='queued'"
    )
    schema = "CREATE TABLE leases (id INTEGER PRIMARY KEY AUTOINCREMENT, deadline REAL, REALLY TEXT)"
    assert translate_schema(schema) == (
        "CREATE TABLE leases (id BIGSERIAL PRIMARY KEY, deadline DOUBLE PRECISION, REALLY TEXT)"
    )
    row = Row({"id": "step", "deadline": 1234.56789})
    assert row[0] == row["id"] == "step"
    assert row[1] == row["deadline"] == 1234.56789
