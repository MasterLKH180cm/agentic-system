"""Strict operator permissions; feature switches never bypass security policy."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


FEATURE_DEFAULTS = MappingProxyType(dict.fromkeys((
    "dag", "agent_loop", "ui", "api_docs", "sse", "webhooks", "mcp", "registry",
    "memory", "artifacts", "approvals", "rag_search", "synthesis", "math", "merge",
    "model_generation", "model_fallback", "telemetry",
), True))
TOOL_FEATURES = MappingProxyType({
    "rag.search": "rag_search", "model.summarize": "synthesis", "math.calculate": "math",
    "data.merge": "merge", "memory.search": "memory", "artifact.publish": "artifacts",
    "agent.run": "agent_loop", "workflow.plan": "dag",
})
MAX_CONFIG_BYTES = 16_384


class FeatureDisabled(PermissionError):
    def __init__(self, feature: str):
        if feature not in FEATURE_DEFAULTS:
            raise ValueError("Unknown feature permission")
        self.feature = feature
        super().__init__(f"Feature disabled: {feature}")

    def detail(self) -> dict:
        return {"code": "feature_disabled", "feature": self.feature, "reason": str(self)}


def _validated(values: object) -> dict[str, bool]:
    if not isinstance(values, Mapping) or any(
        name not in FEATURE_DEFAULTS or type(value) is not bool for name, value in values.items()
    ):
        raise ValueError("Feature configuration requires known names and JSON booleans")
    return dict(values)


def _json_flags(raw: str) -> dict[str, bool]:
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate feature configuration key")
            result[name] = value
        return result

    try:
        if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("Feature configuration exceeds its size limit")
        return _validated(json.loads(raw, object_pairs_hook=unique))
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Feature configuration must be a valid UTF-8 JSON object") from exc


@dataclass(frozen=True)
class Features:
    values: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType({**FEATURE_DEFAULTS, **_validated(self.values)}))

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "Features":
        env = os.environ if environment is None else environment
        values = {}
        filename = env.get("AGENT_FEATURES_FILE")
        if filename:
            try:
                with Path(filename).open("r", encoding="utf-8") as source:
                    raw = source.read(MAX_CONFIG_BYTES + 1)
            except (OSError, UnicodeError) as exc:
                raise ValueError("Cannot read AGENT_FEATURES_FILE as UTF-8") from exc
            values.update(_json_flags(raw))
        if env.get("AGENT_FEATURES_JSON"):
            values.update(_json_flags(env["AGENT_FEATURES_JSON"]))
        return cls(values)

    def effective(self, snapshot: Mapping[str, bool] | None = None) -> dict[str, bool]:
        recorded = {} if snapshot is None else _validated(snapshot)
        return {name: enabled and recorded.get(name, True) for name, enabled in self.values.items()}

    def enabled(self, name: str, snapshot: Mapping[str, bool] | None = None) -> bool:
        if name not in FEATURE_DEFAULTS:
            raise ValueError("Unknown feature permission")
        return self.effective(snapshot)[name]

    def require(self, name: str, snapshot: Mapping[str, bool] | None = None) -> None:
        if not self.enabled(name, snapshot):
            raise FeatureDisabled(name)

    def require_tool(
        self, name: str, *, snapshot: Mapping[str, bool] | None = None,
        online: bool = False, approval_required: bool = False,
    ) -> None:
        if name not in TOOL_FEATURES:
            raise ValueError("Unknown feature-controlled tool")
        self.require(TOOL_FEATURES[name], snapshot)
        if approval_required or name == "artifact.publish":
            self.require("approvals", snapshot)
        if online and name in {"agent.run", "workflow.plan", "model.summarize"}:
            self.require("model_generation", snapshot)

    def tool_enabled(self, name: str, *, online: bool = False) -> bool:
        try:
            self.require_tool(name, online=online)
        except FeatureDisabled:
            return False
        return True
