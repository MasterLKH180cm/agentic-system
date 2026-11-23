"""Configuration. Production requires explicit tenant-scoped credentials."""
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
from urllib.parse import urlparse

from agentic.features import Features

INKLING_MODEL = "thinkingmachines/Inkling-2-Mini"
INKLING_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"
INKLING_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
MODEL_PROVIDERS = frozenset({"offline", "openai", "anthropic", "inkling", "routed"})


def model_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Load an explicitly selected bounded regular PEM bundle with verification."""
    if ca_file is None:
        return ssl.create_default_context()
    if not isinstance(ca_file, str) or not ca_file or len(ca_file) > 4096:
        raise ValueError("Invalid model route CA file")
    try:
        # O_NONBLOCK avoids hanging on a FIFO before fstat can reject it.
        with os.fdopen(os.open(ca_file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 1_048_576:
                raise ValueError("Model route CA file must be a bounded regular PEM file")
            pem = handle.read(1_048_577)
        if len(pem) > 1_048_576:
            raise ValueError("Model route CA file exceeds size limit")
        context = ssl.create_default_context(cadata=pem.decode("ascii"))
    except (OSError, UnicodeError, ssl.SSLError) as exc:
        raise ValueError("Model route CA file must contain valid PEM certificates") from exc
    return context


def validate_reasoning_effort(value: object) -> None:
    if value is None:
        return
    if isinstance(value, str) and value in REASONING_EFFORTS:
        return
    if type(value) in (float, int) and 0 <= value <= 0.99 and math.isfinite(value):
        return
    raise ValueError("reasoning_effort must be a supported name or a finite number within 0..0.99")


def secret(name: str) -> str:
    value = os.getenv(name, "")
    filename = os.getenv(name + "_FILE")
    if filename:
        if value:
            raise ValueError(f"Choose {name} or {name}_FILE, not both")
        value = Path(filename).read_text().strip()
    return value


def validate_model_endpoint(base: str, *, unauthenticated: bool = False) -> None:
    if (not isinstance(base, str) or not base or len(base) > 2048
            or any(char.isspace() or ord(char) < 32 for char in base)):
        raise ValueError("Invalid model route endpoint")
    try:
        parsed = urlparse(base)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in base or "#" in base or port == 0):
            raise ValueError("Invalid model route endpoint")
    except ValueError:
        raise ValueError("Invalid model route endpoint") from None
    if unauthenticated:
        try:
            address = ipaddress.ip_address(parsed.hostname)
            loopback = ("%" not in parsed.hostname and address.is_loopback
                        and getattr(address, "ipv4_mapped", None) is None)
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("Unauthenticated model routes require a literal loopback endpoint")


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
    reasoning_effort: str | float | None = None
    auth_mode: str = "required"
    output_token_parameter: str | None = None
    ca_file: str | None = None

    def __post_init__(self):
        validate_reasoning_effort(self.reasoning_effort)
        if self.reasoning_effort is not None and self.provider != "openai":
            raise ValueError("reasoning_effort requires an OpenAI-compatible route")
        if self.auth_mode not in ("required", "none"):
            raise ValueError("Model route auth_mode must be required or none")
        if not isinstance(self.api_key, str):
            raise ValueError("Invalid model route API key")
        if self.auth_mode == "none" and (self.provider != "openai" or self.api_key):
            raise ValueError("Unauthenticated routes require OpenAI-compatible transport and no API key")
        validate_model_endpoint(self.base_url, unauthenticated=self.auth_mode == "none")
        if self.ca_file is not None:
            if urlparse(self.base_url).scheme != "https":
                raise ValueError("Model route CA file requires HTTPS")
            model_ssl_context(self.ca_file)
        if self.output_token_parameter is not None and (
            self.provider != "openai" or self.output_token_parameter not in ("max_tokens", "max_completion_tokens")
        ):
            raise ValueError("Invalid OpenAI-compatible output_token_parameter")


def model_routes(
    provider: str, openai_key: str | None = None, anthropic_key: str | None = None, *,
    require_credentials: bool = True, credential_loader: Callable[[str], str] | None = None,
) -> tuple[ModelRoute, ...]:
    load_credential = credential_loader or secret
    raw = os.getenv("AGENT_MODEL_ROUTES_JSON")
    if raw:
        entries = json.loads(raw)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
            raise ValueError("AGENT_MODEL_ROUTES_JSON must contain 1..8 routes")
    elif provider == "offline":
        return ()
    else:
        entries = [{"name": "default", "preset": "inkling"}] if provider == "inkling" else [
            {"name": "default", "provider": provider}
        ]
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "name", "provider", "model", "base_url", "api_key_env", "purposes",
            "input_cost_per_million", "output_cost_per_million", "preset", "reasoning_effort",
            "auth_mode", "output_token_parameter", "ca_file",
        }:
            raise ValueError("Invalid model route fields")
        preset = entry.get("preset")
        if preset is not None and preset not in ("inkling", "inkling-local"):
            raise ValueError("Unknown model route preset")
        kind = entry.get("provider", "openai" if preset is not None else provider)
        if preset is not None and kind != "openai":
            raise ValueError("Inkling preset requires an OpenAI-compatible route")
        if kind not in {"openai", "anthropic"}:
            raise ValueError("Route provider must be openai or anthropic")
        model = entry.get("model") or (INKLING_MODEL if preset is not None else os.getenv(
            "OPENAI_CHAT_MODEL" if kind == "openai" else "ANTHROPIC_CHAT_MODEL",
            "gpt-4.1-mini" if kind == "openai" else "claude-sonnet-4-5",
        ))
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("Invalid route model")
        base = entry.get("base_url") or (INKLING_BASE_URL if preset == "inkling" else
                                       INKLING_LOCAL_BASE_URL if preset == "inkling-local" else (
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            if kind == "openai" else os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        ))
        auth_mode = entry.get("auth_mode", "required")
        key_env = entry.get("api_key_env", "TINKER_API_KEY" if preset == "inkling" else None)
        if key_env is not None and (not isinstance(key_env, str)
                                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", key_env)):
            raise ValueError("Invalid route credential environment name")
        if auth_mode == "none" and "api_key_env" in entry:
            raise ValueError("Unauthenticated model routes cannot configure api_key_env")
        if preset == "inkling-local" and auth_mode == "required" and not key_env:
            raise ValueError("Authenticated local Inkling requires an explicit api_key_env")
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
        effort = entry.get("reasoning_effort", "high" if preset == "inkling" else None)
        token_parameter = entry.get("output_token_parameter", "max_tokens" if preset == "inkling-local" else None)
        # Validate transport and auth before reading any credential. In particular,
        # local no-auth routes must never load ambient hosted-provider secrets.
        route = ModelRoute(name, kind, model, base, "", tuple(purposes), *prices, effort,
                           auth_mode, token_parameter, entry.get("ca_file"))
        key = ""
        if require_credentials and auth_mode == "required":
            if key_env:
                key = load_credential(key_env)
            else:
                supplied = openai_key if kind == "openai" else anthropic_key
                key = supplied if supplied is not None else load_credential(
                    "OPENAI_API_KEY" if kind == "openai" else "ANTHROPIC_API_KEY"
                )
            if not key:
                raise ValueError("Configured model route requires an API key")
        result.append(replace(route, base_url=base.rstrip("/"), api_key=key))
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
    model_timeout_seconds: float = 90
    features: Features = field(default_factory=Features)

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in MODEL_PROVIDERS:
            raise ValueError("Unsupported model provider")
        if not isinstance(self.features, Features):
            raise ValueError("Settings features must be validated operator permissions")
        if (type(self.rag_timeout_seconds) not in (int, float)
                or not math.isfinite(self.rag_timeout_seconds) or not 1 <= self.rag_timeout_seconds <= 360):
            raise ValueError("AGENT_RAG_TIMEOUT_SECONDS must be finite and within 1..360")
        if (type(self.model_timeout_seconds) not in (int, float)
                or not 0.1 <= self.model_timeout_seconds <= 360 or not math.isfinite(self.model_timeout_seconds)):
            raise ValueError("AGENT_MODEL_TIMEOUT_SECONDS must be finite and within 0.1..360")
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
        database_url = os.getenv("AGENT_DATABASE_URL")
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("AGENT_DATABASE_URL must select PostgreSQL; use AGENT_DB_PATH for SQLite")
        if database_url:
            from agentic.pgpool import pool_settings
            pool_settings()
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
        if provider not in MODEL_PROVIDERS - {"routed"}:
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
        credentials = {}

        def load_credential(name):
            if name not in credentials:
                credentials[name] = secret(name)
            return credentials[name]

        routes = model_routes(provider, require_credentials=online_allowed, credential_loader=load_credential)
        policies = json.loads(os.getenv("AGENT_TENANT_POLICIES_JSON", "{}"))
        if not isinstance(policies, dict) or any(not isinstance(v, dict) for v in policies.values()):
            raise ValueError("AGENT_TENANT_POLICIES_JSON must map tenants to policy settings")
        profiles = json.loads(os.getenv("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON", "[]"))
        if not isinstance(profiles, list):
            raise ValueError("AGENT_RAG_ADMISSION_PROFILE_IDS_JSON must be an array")
        return cls(
            db_path=database_url or os.getenv("AGENT_DB_PATH", str(Path("data") / "agentic.db")),
            keys=keys,
            rag_url=os.getenv("AGENT_RAG_URL", "http://127.0.0.1:8000").rstrip("/"),
            rag_keys=rag_keys,
            provider="routed" if routes and provider == "offline" else provider,
            openai_key=credentials.get("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
            anthropic_key=credentials.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-5"),
            max_parallel=concurrency,
            lease_seconds=lease, heartbeat_seconds=heartbeat, step_timeout_seconds=timeout,
            max_attempts=attempts, max_model_output_tokens=output_tokens,
            max_model_tokens=token_limit, max_queued_runs=queued,
            routes=routes, tenant_policies=policies,
            rag_admission_policy=os.getenv("AGENT_RAG_ADMISSION_POLICY", "compatibility"),
            rag_admission_profile_ids=tuple(profiles),
            rag_timeout_seconds=float(os.getenv("AGENT_RAG_TIMEOUT_SECONDS", "180")),
            model_timeout_seconds=float(os.getenv("AGENT_MODEL_TIMEOUT_SECONDS", "90")),
            features=features,
        )
