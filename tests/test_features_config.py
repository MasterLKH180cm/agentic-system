"""Strict operator flags, immutable snapshots, and purpose-specific routing."""
from dataclasses import replace
import json
import os

import pytest

from agentic.config import ModelRoute, Settings
from agentic.features import FEATURE_DEFAULTS, MAX_CONFIG_BYTES, TOOL_FEATURES, FeatureDisabled, Features
from agentic.model import ModelGateway

pytestmark = pytest.mark.unit


@pytest.fixture
def database_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("AGENT_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGENT_ENV", "test")
    return monkeypatch


@pytest.mark.parametrize("database_url", [
    "mysql://user:private-password@localhost/app", "sqlite:///local.db",
    "postgresql+psycopg://user:private-password@localhost/app", "postgresql:/localhost/app",
    " data/agentic.db", " ",
])
def test_database_url_rejects_other_schemes_without_disclosing_credentials(database_environment, database_url):
    database_environment.setenv("AGENT_DATABASE_URL", database_url)
    with pytest.raises(ValueError, match="AGENT_DATABASE_URL must select PostgreSQL") as error:
        Settings.from_env()
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_explicit_postgres_url_takes_precedence_over_local_path(database_environment, scheme):
    database_url = f"{scheme}://app@localhost/workflows"
    database_environment.setenv("AGENT_DATABASE_URL", database_url)
    database_environment.setenv("AGENT_DB_PATH", "data/legacy.db")
    assert Settings.from_env().db_path == database_url


@pytest.mark.parametrize("database_url", [None, ""])
def test_unset_database_url_retains_explicit_sqlite_path(database_environment, tmp_path, database_url):
    if database_url is None:
        database_environment.delenv("AGENT_DATABASE_URL", raising=False)
    else:
        database_environment.setenv("AGENT_DATABASE_URL", database_url)
    path = str(tmp_path / "legacy.db")
    database_environment.setenv("AGENT_DB_PATH", path)
    assert Settings.from_env().db_path == path
    assert not (tmp_path / "legacy.db").exists()


def test_manifest_defaults_and_snapshot_permissions_are_immutable():
    expected = {"dag", "agent_loop", "ui", "api_docs", "sse", "webhooks", "mcp", "registry",
                "memory", "artifacts", "approvals", "rag_search", "synthesis", "math", "merge",
                "model_generation", "model_fallback", "telemetry"}
    assert set(FEATURE_DEFAULTS) == expected
    assert Features.from_env({}).effective() == dict.fromkeys(expected, True)
    assert Features.from_env({"AGENT_FEATURES_FILE": "", "AGENT_FEATURES_JSON": ""}).effective() == dict.fromkeys(expected, True)
    source = {"memory": False}
    flags = Features(source)
    source["memory"] = True
    with pytest.raises(TypeError):
        flags.values["memory"] = True
    assert not flags.enabled("memory")
    assert flags.effective({"memory": True, "synthesis": False})["memory"] is False
    assert flags.effective({"memory": True, "synthesis": False})["synthesis"] is False
    assert flags.enabled("math", {})  # Legacy runs have no additional ceiling.
    with pytest.raises(ValueError, match="Unknown"):
        flags.enabled("authentication")
    with pytest.raises(ValueError, match="known names"):
        flags.effective({"memory": 1})
    with pytest.raises(ValueError, match="Unknown"):
        FeatureDisabled("operator-secret")


def test_file_then_inline_precedence_and_no_environment_mutation(tmp_path):
    file = tmp_path / "features.json"
    file.write_text('{"memory":false,"ui":false}', encoding="utf-8")
    environment = {"AGENT_FEATURES_FILE": str(file), "AGENT_FEATURES_JSON": '{"ui":true,"dag":false}'}
    original = environment.copy()
    flags = Features.from_env(environment)
    assert not flags.enabled("memory") and not flags.enabled("dag") and flags.enabled("ui")
    assert flags.enabled("agent_loop")
    assert environment == original
    assert not Features.from_env({"AGENT_FEATURES_FILE": str(file)}).enabled("ui")


@pytest.mark.parametrize("raw", [
    "[]", "null", "42", '"private-value"', '{"math":0}', '{"math":1}',
    '{"math":"false"}', '{"math":null}', '{"math":[]}', '{"math":{}}',
    '{"private-unknown-flag":true}', '{"math":true,"math":false}', '{"math":false',
    " " * (MAX_CONFIG_BYTES + 1), '\ud800',
])
def test_invalid_inline_configuration_fails_without_echoing_values(raw):
    with pytest.raises(ValueError) as error:
        Features.from_env({"AGENT_FEATURES_JSON": raw})
    assert "private" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "directory", "unicode", "oversized", "malformed"])
def test_selected_file_errors_fail_closed(tmp_path, kind):
    path = tmp_path / "features.json"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes({"unicode": b"\xff", "oversized": b" " * (MAX_CONFIG_BYTES + 1),
                          "malformed": b'{"math":'}[kind])
    with pytest.raises(ValueError):
        Features.from_env({"AGENT_FEATURES_FILE": str(path), "AGENT_FEATURES_JSON": "{}"})


@pytest.mark.parametrize("tool,feature", list(TOOL_FEATURES.items()))
def test_tool_permissions_are_explicit_and_report_fixed_codes(tool, feature):
    disabled = Features({feature: False})
    with pytest.raises(FeatureDisabled) as error:
        disabled.require_tool(tool)
    assert error.value.detail() == {
        "code": "feature_disabled", "feature": feature, "reason": "Feature disabled: " + feature,
    }
    assert not disabled.tool_enabled(tool)
    assert Features().tool_enabled(tool)


def test_approval_and_generation_are_additional_requirements():
    with pytest.raises(FeatureDisabled, match="approvals"):
        Features({"approvals": False}).require_tool("math.calculate", approval_required=True)
    with pytest.raises(FeatureDisabled, match="approvals"):
        Features({"approvals": False}).require_tool("artifact.publish")
    with pytest.raises(FeatureDisabled, match="model_generation"):
        Features({"model_generation": False}).require_tool("workflow.plan", online=True)
    Features({"model_generation": False}).require_tool("model.summarize", online=False)
    with pytest.raises(ValueError, match="Unknown"):
        Features().require_tool("shell.execute")


def test_model_routes_keep_purpose_order_and_snapshot_fallback_ceiling(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    routes = tuple(ModelRoute(name, "openai", "fixture", "http://fixture.invalid/v1", "fixture-key", purposes)
                   for name, purposes in (("planning-only", ("planning",)), ("primary", ("synthesis",)),
                                          ("fallback", ("planning", "synthesis"))))
    settings = replace(Settings.from_env(), routes=routes)
    model = ModelGateway(settings, store=object())
    assert [route.name for route in model.routes("synthesis")] == ["primary", "fallback"]
    assert [route.name for route in model.routes("planning", {"model_fallback": False})] == ["planning-only"]
    model.config = replace(settings, features=Features({"model_fallback": False}))
    assert [route.name for route in model.routes("synthesis", {"model_fallback": True})] == ["primary"]
    with pytest.raises(ValueError, match="No model route"):
        model.routes("evaluation")


def test_disabled_generation_retains_provider_without_loading_unused_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_FEATURES_JSON", '{"model_generation":false}')
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(tmp_path / "unmounted-openai-key"))
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY_FILE", str(tmp_path / "unmounted-custom-key"))
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([
        {"name": "operator-route", "provider": "anthropic", "api_key_env": "CUSTOM_PROVIDER_KEY"},
    ]))
    settings = Settings.from_env()
    assert settings.provider == "openai"
    assert settings.openai_key == settings.anthropic_key == settings.routes[0].api_key == ""
    assert settings.routes[0].provider == "anthropic"
    with pytest.raises(ValueError, match="validated"):
        replace(settings, features={"memory": False})
    monkeypatch.setenv("AGENT_FEATURES_JSON", '{"model_generation":true}')
    with pytest.raises((OSError, ValueError)):
        Settings.from_env()
