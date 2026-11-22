"""Configuration. Production requires explicit tenant-scoped credentials."""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse

from agentic.features import Features


def secret(name: str) -> str:
    value = os.getenv(name, "")
    filename = os.getenv(name + "_FILE")
    if filename:
        if value:
            raise ValueError(f"Choose {name} or {name}_FILE, not both")
        value = Path(filename).read_text().strip()
    return value


@dataclass(frozen=True)
class ModelRoute:
    name: str
    provider: str
    model: str
    base_url: str
    api_key: str
    purposes: tuple[str, ...] = ("planning", "synthesis", "evaluation")
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


def model_routes(
    provider: str, openai_key: str, anthropic_key: str, *, require_credentials: bool = True,
) -> tuple[ModelRoute, ...]:
    raw = os.getenv("AGENT_MODEL_ROUTES_JSON")
    if raw:
        entries = json.loads(raw)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
            raise ValueError("AGENT_MODEL_ROUTES_JSON must contain 1..8 routes")
    elif provider == "offline":
        return ()
    else:
        entries = [{"name": "default", "provider": provider}]
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "name", "provider", "model", "base_url", "api_key_env", "purposes",
            "input_cost_per_million", "output_cost_per_million",
        }:
            raise ValueError("Invalid model route fields")
        kind = entry.get("provider", provider)
        if kind not in {"openai", "anthropic"}:
            raise ValueError("Route provider must be openai or anthropic")
        model = entry.get("model") or os.getenv(
            "OPENAI_CHAT_MODEL" if kind == "openai" else "ANTHROPIC_CHAT_MODEL",
            "gpt-4.1-mini" if kind == "openai" else "claude-sonnet-4-5",
        )
        base = entry.get("base_url") or (
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            if kind == "openai" else os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        )
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid model route endpoint")
        key = secret(entry["api_key_env"]) if require_credentials and entry.get("api_key_env") else (
            openai_key if kind == "openai" else anthropic_key
        )
        if require_credentials and not key:
            raise ValueError("Configured model route requires an API key")
        prices = [entry.get("input_cost_per_million"), entry.get("output_cost_per_million")]
        if any(p is not None and (type(p) not in (float, int) or not 0 <= p <= 1_000_000 or not math.isfinite(p))
               for p in prices):
            raise ValueError("Route prices must be finite nonnegative numbers")
        purposes = entry.get("purposes", ["planning", "synthesis", "evaluation"])
        if not isinstance(purposes, list) or not purposes or not all(
            p in {"planning", "synthesis", "evaluation"} for p in purposes
        ):
            raise ValueError("Invalid route purposes")
        name = entry.get("name", f"route-{len(result) + 1}")
        if not isinstance(name, str) or not name or len(name) > 80:
            raise ValueError("Invalid route name")
        result.append(ModelRoute(name, kind, model, base.rstrip("/"), key, tuple(purposes), *prices))
    if len({r.name for r in result}) != len(result):
        raise ValueError("Model route names must be unique")
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    db_path: str
    keys: dict[str, str]
    rag_url: str
    rag_keys: dict[str, str]
    provider: str
    openai_key: str
    openai_base_url: str
    openai_model: str
    anthropic_key: str
    anthropic_model: str
    max_parallel: int
    lease_seconds: float = 180
    heartbeat_seconds: float = 30
    step_timeout_seconds: float = 300
    max_attempts: int = 3
    max_model_output_tokens: int = 1000
    max_model_tokens: int = 12000
    max_queued_runs: int = 100
    routes: tuple[ModelRoute, ...] = ()
    tenant_policies: dict[str, dict] = field(default_factory=dict)
    rag_admission_policy: str = "compatibility"
    rag_admission_profile_ids: tuple[str, ...] = ()
    rag_timeout_seconds: float = 180
    features: Features = field(default_factory=Features)

    def __post_init__(self):
        if not isinstance(self.features, Features):
            raise ValueError("Settings features must be validated operator permissions")
        if (type(self.rag_timeout_seconds) not in (int, float)
                or not math.isfinite(self.rag_timeout_seconds) or not 1 <= self.rag_timeout_seconds <= 360):
            raise ValueError("AGENT_RAG_TIMEOUT_SECONDS must be finite and within 1..360")
        if self.rag_admission_policy not in {"compatibility", "calibrated_required"}:
            raise ValueError("Unknown RAG admission policy")
        profiles = self.rag_admission_profile_ids
        if not isinstance(profiles, tuple) or len(profiles) > 32 or any(
            not isinstance(value, str) or not 1 <= len(value) <= 100 or value != value.strip()
            or any(ord(char) < 32 for char in value) for value in profiles
        ) or len(set(profiles)) != len(profiles):
            raise ValueError("RAG admission profile IDs must be distinct bounded identifiers")
        if self.rag_admission_policy == "calibrated_required" and not profiles:
            raise ValueError("Required RAG admission needs a nonempty profile allowlist")

    @classmethod
    def from_env(cls) -> "Settings":
        features = Features.from_env()
        env = os.getenv("AGENT_ENV", "production").lower()
        try:
            keys = json.loads(os.getenv("AGENT_KEYS_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("AGENT_KEYS_JSON must be valid JSON") from exc
        if not isinstance(keys, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in keys.items()
        ):
            raise ValueError("AGENT_KEYS_JSON must map API keys to tenant names")
        if env in {"development", "test"} and not keys:
            keys = {os.getenv("AGENT_API_KEY", "local-agent-key"): "local"}
        oidc = [os.getenv("AGENT_OIDC_ISSUER"), os.getenv("AGENT_OIDC_AUDIENCE"),
                os.getenv("AGENT_OIDC_JWKS_URL")]
        if any(oidc) and not all(oidc):
            raise ValueError("OIDC requires issuer, audience and JWKS URL")
        if not keys and not all(oidc):
            raise ValueError("AGENT_KEYS_JSON required in production")
        if env == "production" and any(len(key) < 24 for key in keys):
            raise ValueError("Use strong production API keys")
        provider = os.getenv("AGENT_MODEL_PROVIDER", "offline").lower()
        if provider not in {"offline", "openai", "anthropic"}:
            raise ValueError("Unsupported model provider")
        concurrency = int(os.getenv("AGENT_MAX_PARALLEL", "4"))
        if not 1 <= concurrency <= 16:
            raise ValueError("AGENT_MAX_PARALLEL must be 1..16")
        try:
            rag_keys = json.loads(os.getenv("AGENT_RAG_KEYS_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("AGENT_RAG_KEYS_JSON must be valid JSON") from exc
        if not isinstance(rag_keys, dict) or any(
            not isinstance(t, str) or not t or not isinstance(k, str) or not k
            for t, k in rag_keys.items()
        ):
            raise ValueError("AGENT_RAG_KEYS_JSON must map tenant IDs to RAG API keys")
        if env in {"development", "test"} and not rag_keys and set(keys.values()) == {"local"}:
            rag_keys = {"local": os.getenv("AGENT_RAG_API_KEY", "local-development-key")}
        if any(t not in rag_keys for t in keys.values()):
            raise ValueError("Every configured tenant requires a tenant-specific RAG key")
        if len(set(rag_keys.values())) != len(rag_keys):
            raise ValueError("Different tenants must not share a RAG API key")
        if all(oidc) and not rag_keys:
            raise ValueError("OIDC requires configured tenant RAG credentials")
        lease = float(os.getenv("AGENT_LEASE_SECONDS", "180"))
        heartbeat = float(os.getenv("AGENT_HEARTBEAT_SECONDS", str(min(30, lease / 4))))
        timeout = float(os.getenv("AGENT_STEP_TIMEOUT_SECONDS", "300"))
        attempts = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))
        output_tokens = int(os.getenv("AGENT_MAX_MODEL_OUTPUT_TOKENS", "1000"))
        token_limit = int(os.getenv("AGENT_MAX_MODEL_TOKENS", "12000"))
        queued = int(os.getenv("AGENT_MAX_QUEUED_RUNS", "100"))
        if not all(math.isfinite(n) for n in (lease, heartbeat, timeout)) or not (
            0.1 <= lease <= 3600 and 0 < heartbeat < lease / 3 and 0.1 <= timeout <= 3600
            and 1 <= attempts <= 10 and 1 <= output_tokens <= 8000
            and 1 <= token_limit <= 200000 and 1 <= queued <= 10000
        ):
            raise ValueError("Invalid execution limits")
        online_allowed = features.enabled("model_generation")
        openai_key, anthropic_key = (secret("OPENAI_API_KEY"), secret("ANTHROPIC_API_KEY")) if online_allowed else ("", "")
        routes = model_routes(provider, openai_key, anthropic_key, require_credentials=online_allowed)
        policies = json.loads(os.getenv("AGENT_TENANT_POLICIES_JSON", "{}"))
        if not isinstance(policies, dict) or any(not isinstance(v, dict) for v in policies.values()):
            raise ValueError("AGENT_TENANT_POLICIES_JSON must map tenants to policy settings")
        profiles = json.loads(os.getenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", "[]"))
        if not isinstance(profiles, list):
            raise ValueError("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON must be an array")
        return cls(
            db_path=os.getenv("AGENT_DATABASE_URL") or os.getenv("AGENT_DB_PATH", str(Path("data") / "agentic.db")),
            keys=keys,
            rag_url=os.getenv("AGENT_RAG_URL", "http://127.0.0.1:8000").rstrip("/"),
            rag_keys=rag_keys,
            provider="routed" if routes and provider == "offline" else provider,
            openai_key=openai_key,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
            anthropic_key=anthropic_key,
            anthropic_model=os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-5"),
            max_parallel=concurrency,
            lease_seconds=lease, heartbeat_seconds=heartbeat, step_timeout_seconds=timeout,
            max_attempts=attempts, max_model_output_tokens=output_tokens,
            max_model_tokens=token_limit, max_queued_runs=queued,
            routes=routes, tenant_policies=policies,
            rag_admission_policy=os.getenv("AGENT_RAG_ADMISSION_POLICY", "compatibility"),
            rag_admission_profile_ids=tuple(profiles),
            rag_timeout_seconds=float(os.getenv("AGENT_RAG_TIMEOUT_SECONDS", "180")),
            features=features,
        )
