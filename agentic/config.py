"""Configuration. Production requires explicit tenant-scoped credentials."""
from dataclasses import dataclass
import json
import os
from pathlib import Path


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

    @classmethod
    def from_env(cls) -> "Settings":
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
        if not keys:
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
        return cls(
            db_path=os.getenv("AGENT_DB_PATH", str(Path("data") / "agentic.db")),
            keys=keys,
            rag_url=os.getenv("AGENT_RAG_URL", "http://127.0.0.1:8000").rstrip("/"),
            rag_keys=rag_keys,
            provider=provider,
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-5"),
            max_parallel=concurrency,
        )
