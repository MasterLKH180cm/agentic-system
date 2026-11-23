"""Pure model profile and capability configuration contracts."""
import json

import pytest

from agentic.config import INKLING_BASE_URL, INKLING_LOCAL_BASE_URL, INKLING_MODEL, ModelRoute, Settings, model_routes

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


def test_local_preset_does_not_load_any_ambient_provider_secret(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", '[{"preset":"inkling-local","auth_mode":"none"}]')
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TINKER_API_KEY"):
        monkeypatch.setenv(name, "ambient-key-must-not-be-used")
        monkeypatch.setenv(name + "_FILE", "/missing/ambient-secret-must-not-be-read")

    def forbidden(name):
        raise AssertionError("No local credential lookup is permitted")

    monkeypatch.setattr("agentic.config.secret", forbidden)
    settings = Settings.from_env()
    route, = settings.routes
    assert settings.provider == "routed"
    assert settings.openai_key == settings.anthropic_key == route.api_key == ""
    assert (route.provider, route.model, route.base_url, route.auth_mode,
            route.reasoning_effort, route.output_token_parameter) == (
        "openai", INKLING_MODEL, INKLING_LOCAL_BASE_URL, "none", None, "max_tokens",
    )


def test_local_authenticated_preset_requires_explicit_credential_name(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", '[{"preset":"inkling-local"}]')
    with pytest.raises(ValueError, match="explicit api_key_env"):
        model_routes("offline", "ambient-openai-key", "ambient-anthropic-key")


def test_authenticated_local_route_loads_only_its_selected_secret(monkeypatch, tmp_path):
    credential = tmp_path / "local-key"
    credential.write_text("dedicated-local-key\n")
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "preset": "inkling-local", "base_url": "http://inference:8080/v1", "api_key_env": "LOCAL_INKLING_API_KEY",
    }]))
    monkeypatch.setenv("LOCAL_INKLING_API_KEY_FILE", str(credential))
    monkeypatch.delenv("LOCAL_INKLING_API_KEY", raising=False)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TINKER_API_KEY"):
        monkeypatch.setenv(name + "_FILE", "/missing/unused-secret")
    settings = Settings.from_env()
    route, = settings.routes
    assert route.auth_mode == "required" and route.api_key == "dedicated-local-key"
    assert settings.openai_key == settings.anthropic_key == ""


@pytest.mark.parametrize("base", ["http://127.0.0.1:8080/v1", "https://127.0.0.2/v1", "http://[::1]:8080/v1"])
def test_no_auth_accepts_only_clean_literal_loopback_addresses(base):
    route = ModelRoute("local", "openai", INKLING_MODEL, base, "", auth_mode="none")
    assert route.auth_mode == "none"


@pytest.mark.parametrize("base", [
    "http://localhost:8080/v1", "http://inference:8080/v1", "http://host.docker.internal:8080/v1",
    "http://192.168.1.1/v1", "https://example.invalid/v1", "https://8.8.8.8/v1", "http://0.0.0.0/v1",
    "http://127.1/v1", "http://2130706433/v1", "http://127.0.0.1.example.invalid/v1",
    "http://[::ffff:127.0.0.1]/v1", "http://[::1%25lo]/v1", "http://user@127.0.0.1/v1",
    "http://@127.0.0.1/v1", "http://127.0.0.1/v1?", "http://127.0.0.1/v1#",
    "http://127.0.0.1:0/v1", "http://127.0.0.1:65536/v1", "http://127.0.0.1:invalid/v1",
    " http://127.0.0.1/v1", "http://127.0.0.1/v1\n", "file:///127.0.0.1/v1",
])
def test_no_auth_rejects_unsafe_or_ambiguous_endpoint(base):
    with pytest.raises(ValueError, match="endpoint"):
        ModelRoute("local", "openai", INKLING_MODEL, base, "", auth_mode="none")


@pytest.mark.parametrize("key_env", [None, "LOCAL_INKLING_API_KEY"])
def test_no_auth_rejects_a_credential_selector_even_when_empty(monkeypatch, key_env):
    monkeypatch.setenv("AGENT_MODEL_ROUTES_JSON", json.dumps([{
        "preset": "inkling-local", "auth_mode": "none", "api_key_env": key_env,
    }]))
    with pytest.raises(ValueError, match="cannot configure api_key_env"):
        model_routes("offline", "", "")


@pytest.mark.parametrize("changes", [
    {"api_key": "contradictory"}, {"provider": "anthropic"}, {"auth_mode": True}, {"auth_mode": None},
    {"auth_mode": "optional"}, {"output_token_parameter": "max_new_tokens"}, {"output_token_parameter": True},
])
def test_local_route_rejects_contradictory_auth_and_unknown_output_fields(changes):
    values = dict(name="local", provider="openai", model=INKLING_MODEL,
                  base_url=INKLING_LOCAL_BASE_URL, api_key="", auth_mode="none")
    with pytest.raises(ValueError):
        ModelRoute(**{**values, **changes})


def test_output_token_parameter_is_openai_only():
    with pytest.raises(ValueError, match="output_token_parameter"):
        ModelRoute("local", "anthropic", "model", "https://example.invalid", "key",
                   output_token_parameter="max_tokens")


def test_selected_hosted_credentials_remain_available_and_unrelated_secrets_are_not_read(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.delenv("AGENT_MODEL_ROUTES_JSON", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "selected-hosted-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", "/missing/unselected-secret")
    settings = Settings.from_env()
    assert settings.openai_key == settings.routes[0].api_key == "selected-hosted-key"
    assert settings.anthropic_key == ""
    assert settings.routes[0].auth_mode == "required"
