"""Offline schema and route capability boundaries without provider requests."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agentic.config import ModelRoute, model_routes
from agentic.model import ModelGateway, response_format

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
          "properties": {"answer": {"type": "string"}}}


@pytest.mark.parametrize("model", ["qwen/Qwen3-1.7B", "gateway/qwen3-1.7b-q8_0-v1", "arbitrary"])
def test_schema_capability_is_never_inferred_from_model_name(model):
    route = ModelRoute("local", "openai", model, "http://127.0.0.1/v1", "", auth_mode="none")
    assert route.supports_json_schema is False
    assert replace(route, supports_json_schema=True).supports_json_schema is True


@pytest.mark.parametrize("flag", [None, 0, 1, "true", "false", [], {}])
def test_schema_route_flag_requires_a_real_boolean_before_credentials(monkeypatch, flag):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "provider": "openai", "model": "custom", "base_url": "https://example.invalid/v1",
        "api_key_env": "UNREAD_MODEL_KEY", "supports_json_schema": flag,
    }]))
    with pytest.raises(ValueError, match="boolean"):
        model_routes("offline", credential_loader=lambda _: pytest.fail("Must validate before loading secrets"))


def test_schema_opt_in_loads_from_routes_and_rejects_other_transport(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "provider": "openai", "model": "custom", "base_url": "http://127.0.0.1/v1",
        "auth_mode": "none", "supports_json_schema": True,
    }]))
    route, = model_routes("offline", credential_loader=lambda _: pytest.fail("No local credential"))
    assert route.supports_json_schema
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        ModelRoute("other", "anthropic", "custom", "https://example.invalid/v1", "key", supports_json_schema=True)


def test_valid_local_refs_are_snapshotted_without_losing_schema_constraints():
    schema = {"type": "object", "properties": {"answer": {"$ref": "#/$defs/Answer"}},
              "required": ["answer"], "additionalProperties": False,
              "$defs": {"Answer": {"type": "string", "const": "unchanged"}}}
    original = deepcopy(schema)
    actual = response_format(schema)
    assert actual == {"type": "json_schema", "json_schema": {
        "name": "agentic_response", "strict": True, "schema": original}}
    schema["$defs"]["Answer"]["const"] = "mutated"
    assert actual["json_schema"]["schema"] == original


@pytest.mark.parametrize("schema", [
    [], {"type": "array"}, {"type": "object", "description": "private-schema" * 2000},
    {"type": "object", "description": "中" * 6000},
    {"type": "object", "description": "\u0000" * 4000},
    {"type": "object", "description": "\ud800"},
    {"type": "object", 1: "invalid-key"}, {"type": "object", "default": object()},
    {"type": "object", "maximum": float("nan")},
    {"type": "object", "properties": {"private-schema": {"type": "unknown-type"}}},
    {"type": "object", "$ref": "https://private.invalid/schema"},
    {"type": "object", "$ref": "#"}, {"type": "object", "$ref": "#/$defs/absent"},
    {"type": "object", "$ref": "#/$defs/a~2b"}, {"type": "object", "$ref": "#/%24defs/answer"},
    {"type": "object", "$ref": 2},
    {"type": "object", "$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/A"}}},
    {"type": "object", "$ref": "#/$defs/A", "$defs": {"A": "not-a-schema"}},
    {"type": "object", "enum": list(range(2048))},
    {"type": "object", "enum": list(range(2049))},
    {"type": "object", "properties": {str(i): {} for i in range(2049)}},
])
def test_invalid_or_oversized_schemas_fail_without_disclosing_data(schema):
    with pytest.raises(ValueError, match="structured response schema") as error:
        response_format(schema)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("keyword", ["$id", "$schema", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor"])
def test_schema_resource_or_dynamic_reference_changes_are_rejected(keyword):
    with pytest.raises(ValueError, match="structured response schema"):
        response_format({"type": "object", keyword: "https://private.invalid/schema"})


def test_schema_depth_python_cycles_and_exponential_reference_expansion_are_bounded():
    deeply_nested = node = {"type": "object"}
    for _ in range(40):
        node["properties"] = {"next": {"type": "object"}}
        node = node["properties"]["next"]
    cyclic = {"type": "object"}
    cyclic["properties"] = {"self": cyclic}
    definitions = {"A0": {"type": "string"}}
    for index in range(1, 14):
        definitions[f"A{index}"] = {"anyOf": [{"$ref": f"#/$defs/A{index - 1}"} for _ in range(2)]}
    expanded = {"type": "object", "properties": {"answer": {"$ref": "#/$defs/A13"}}, "$defs": definitions}
    for schema in (deeply_nested, cyclic, expanded):
        with pytest.raises(ValueError, match="structured response schema"):
            response_format(schema)


def test_input_accounting_includes_the_complete_response_format_wrapper():
    wrapper = json.dumps({"response_format": response_format(SCHEMA)}, ensure_ascii=False, separators=(",", ":"))
    assert ModelGateway.input_units("System", "中文", response_schema=SCHEMA) == (
        ModelGateway.input_units("System", "中文") + len(wrapper.encode("utf-8")))
