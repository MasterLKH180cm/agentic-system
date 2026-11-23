"""Pure model profile and capability configuration contracts."""
import json

import pytest

from agentic.config import INKLING_BASE_URL, INKLING_MODEL, ModelRoute, Settings, model_routes

pytestmark = pytest.mark.unit


def test_inkling_preset_uses_dedicated_credentials_and_explicit_capabilities(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("TINKER_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other.invalid/v1")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "other-model")
    route, = model_routes("inkling", "unrelated-key", "")
    assert (route.provider, route.model, route.base_url, route.api_key, route.reasoning_effort) == (
        "openai", INKLING_MODEL, INKLING_BASE_URL, "fixture-key", "high",
    )


def test_explicit_preset_can_target_authorized_compatible_server(monkeypatch):
    monkeypatch.setenv("INKLING_TEST_KEY", "fixture-key")
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "name": "local-inkling", "preset": "inkling", "base_url": "http://127.0.0.1:8080/v1",
        "model": "operator-alias", "api_key_env": "INKLING_TEST_KEY", "reasoning_effort": 0.2,
    }]))
    route, = model_routes("offline", "", "")
    assert route.model == "operator-alias" and route.reasoning_effort == 0.2
    assert route.base_url == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", 0, 0.1, 0.99, None])
def test_documented_reasoning_efforts_are_accepted(effort):
    route = ModelRoute("test", "openai", INKLING_MODEL, INKLING_BASE_URL, "fixture", reasoning_effort=effort)
    assert route.reasoning_effort == effort


@pytest.mark.parametrize("effort", [True, False, -0.1, 1, 10**1000, float("nan"), float("inf"), "extreme", {}, []])
def test_invalid_reasoning_effort_fails_before_any_request(effort):
    with pytest.raises(ValueError, match="reasoning_effort"):
        ModelRoute("test", "openai", INKLING_MODEL, INKLING_BASE_URL, "fixture", reasoning_effort=effort)


def test_anthropic_cannot_silently_ignore_openai_reasoning_effort():
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        ModelRoute("test", "anthropic", "model", "https://example.invalid", "fixture", reasoning_effort="high")


@pytest.mark.parametrize("override", [
    {"preset": "unknown"}, {"preset": "inkling", "provider": "anthropic"},
    {"model": 1}, {"base_url": 42}, {"base_url": "https://example.invalid/v1?key=secret"},
    {"base_url": "https://example.invalid/v1#fragment"}, {"api_key_env": 3},
])
def test_invalid_profile_fields_fail_closed(monkeypatch, override):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{"provider": "openai", **override}]))
    with pytest.raises(ValueError):
        model_routes("openai", "fixture", "")


def test_inkling_environment_is_supported_without_overriding_embedding_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "inkling")
    monkeypatch.setenv("TINKER_API_KEY", "fixture-key")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    config = Settings.from_env()
    assert config.routes[0].model == INKLING_MODEL
    assert config.provider == "inkling"


def test_disabled_generation_does_not_read_inkling_secret_file(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("TINKER_API_KEY_FILE", "/missing/credential")
    route, = model_routes("inkling", "", "", require_credentials=False)
    assert route.api_key == "" and route.model == INKLING_MODEL


@pytest.mark.parametrize("value", [0, 0.09, 361, True, float("nan"), float("inf"), 10**1000])
def test_model_timeout_configuration_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="AGENT_MODEL_TIMEOUT_SECONDS"):
        Settings(db_path=":memory:", keys={}, rag_url="http://127.0.0.1:1", rag_keys={}, provider="offline",
                 openai_key="", openai_base_url="https://example.invalid", openai_model="",
                 anthropic_key="", anthropic_model="", max_parallel=1, model_timeout_seconds=value)


def test_model_timeout_is_operator_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.setenv("AGENT_MODEL_TIMEOUT_SECONDS", "120")
    assert Settings.from_env().model_timeout_seconds == 120
