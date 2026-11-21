"""Bounded domain contracts shared by submission, planning and execution."""
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$"
ToolName = Literal[
    "rag.search", "model.summarize", "math.calculate", "data.merge",
    "artifact.publish", "memory.search", "agent.run", "workflow.plan",
]
READ_ONLY_TOOLS = {
    "rag.search", "model.summarize", "math.calculate", "data.merge", "memory.search",
}
ALL_TOOLS = READ_ONLY_TOOLS | {"artifact.publish", "agent.run"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RowPredicate(StrictModel):
    field: str = Field(min_length=1, max_length=128)
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"]
    value: str | float | int | bool | list | None = None

    @model_validator(mode="after")
    def valid_value(self):
        values = self.value if isinstance(self.value, list) else [self.value]
        if len(values) > 100 or any(type(v) not in (str, float, int, bool, type(None)) for v in values):
            raise ValueError("Row predicate requires at most 100 scalar values")
        if any(type(v) is float and not math.isfinite(v) for v in values):
            raise ValueError("Row predicate requires finite numbers")
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("The in predicate requires a list")
        return self


class Aggregation(StrictModel):
    operation: Literal["count", "sum", "mean", "min", "max"]
    field: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_field(self):
        if self.operation != "count" and self.field is None:
            raise ValueError("Numeric aggregation requires a field")
        return self


class SearchArgs(StrictModel):
    question: str | None = Field(default=None, min_length=2, max_length=2000)
    mode: str = Field(default="hybrid", pattern=r"^[a-z][a-z0-9_]{0,39}$")
    top_k: int = Field(default=5, ge=1, le=10)
    row_filters: list[RowPredicate] = Field(default_factory=list, max_length=20)
    aggregate: Aggregation | None = None

    @model_validator(mode="after")
    def structured_only(self):
        if (self.row_filters or self.aggregate is not None) and self.mode != "structured":
            raise ValueError("Structured arguments require structured mode")
        return self


class SummaryArgs(StrictModel):
    question: str | None = Field(default=None, min_length=2, max_length=2000)


class CalculateArgs(StrictModel):
    expression: str = Field(min_length=1, max_length=120)


class MemorySearchArgs(StrictModel):
    namespace: str = Field(default="default", pattern=NAME_PATTERN)
    query: str | None = Field(default=None, min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=10)


class EmptyArgs(StrictModel):
    pass


ARGUMENT_MODELS = {
    "rag.search": SearchArgs,
    "model.summarize": SummaryArgs,
    "math.calculate": CalculateArgs,
    "data.merge": EmptyArgs,
    "artifact.publish": EmptyArgs,
    "memory.search": MemorySearchArgs,
    "agent.run": EmptyArgs,
    "workflow.plan": EmptyArgs,
}


class Step(StrictModel):
    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    tool: ToolName
    args: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    approval_required: bool = False

    @model_validator(mode="after")
    def validate_step(self) -> "Step":
        if len(json.dumps(self.args)) > 8_000:
            raise ValueError("Step arguments exceed 8000 characters")
        ARGUMENT_MODELS[self.tool].model_validate(self.args)
        if self.tool == "artifact.publish":
            self.approval_required = True
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Duplicate dependencies")
        return self


def validate_dag(steps: list[Step]) -> None:
    if not steps:
        raise ValueError("Explicit workflow must contain at least one step")
    names = {s.id for s in steps}
    if len(names) != len(steps):
        raise ValueError("Duplicate step ID")
    dependencies = {s.id: set(s.depends_on) for s in steps}
    for name, deps in dependencies.items():
        if name in deps or not deps <= names:
            raise ValueError("Self or unknown step dependency")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("Cyclic workflow")
        if name in visited:
            return
        visiting.add(name)
        for dep in dependencies[name]:
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)


class WorkflowIn(StrictModel):
    goal: str = Field(min_length=3, max_length=2000)
    steps: list[Step] | None = Field(default=None, max_length=24)
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    runtime_mode: Literal["dag", "agent_loop"] = "dag"
    agent_id: str | None = Field(default=None, pattern=NAME_PATTERN)
    agent_version: int | None = Field(default=None, ge=1)
    policy_id: str | None = Field(default=None, pattern=NAME_PATTERN)
    policy_version: int | None = Field(default=None, ge=1)
    workflow_id: str | None = Field(default=None, pattern=NAME_PATTERN)
    workflow_version: int | None = Field(default=None, ge=1)
    memory_namespace: str = Field(default="default", pattern=NAME_PATTERN)
    max_rounds: int = Field(default=3, ge=1, le=5)
    max_model_tokens: int = Field(default=12000, ge=1, le=200000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_workflow(self) -> "WorkflowIn":
        for name in ("agent", "policy", "workflow"):
            if getattr(self, name + "_version") is not None and getattr(self, name + "_id") is None:
                raise ValueError(f"{name}_version requires {name}_id")
        if self.steps is not None:
            validate_dag(self.steps)
            if self.runtime_mode == "agent_loop":
                raise ValueError("agent_loop plans its steps; do not supply explicit steps")
            if any(s.tool in {"agent.run", "workflow.plan"} for s in self.steps):
                raise ValueError("Internal runtime steps cannot be submitted directly")
        if self.workflow_id and (self.steps is not None or self.runtime_mode != "dag"):
            raise ValueError("Workflow definitions require DAG mode without explicit steps")
        return self


class ApprovalIn(StrictModel):
    approved: bool
    reason: str = Field(default="", max_length=500)


class AgentDefinition(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    version: int = Field(ge=1)
    instructions: str = Field(default="", max_length=4000)
    allowed_tools: list[ToolName] = Field(
        default_factory=lambda: sorted(READ_ONLY_TOOLS), min_length=1, max_length=7
    )
    evaluator: Literal["evidence", "arithmetic"] = "evidence"
    memory_namespace: str = Field(default="default", pattern=NAME_PATTERN)
    max_rounds: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def read_only(self) -> "AgentDefinition":
        if not set(self.allowed_tools) <= READ_ONLY_TOOLS:
            raise ValueError("Adaptive agents may only use registered read-only tools")
        return self


class WorkflowDefinition(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    version: int = Field(ge=1)
    steps: list[Step] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def dag(self) -> "WorkflowDefinition":
        validate_dag(self.steps)
        if any(s.tool in {"agent.run", "workflow.plan"} for s in self.steps):
            raise ValueError("Nested runtime steps are not allowed in workflow definitions")
        return self


class PolicyDefinition(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    version: int = Field(ge=1)
    allowed_tools: list[ToolName] = Field(default_factory=lambda: sorted(ALL_TOOLS), min_length=1)
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["default"], min_length=1, max_length=30)
    max_steps: int = Field(default=24, ge=1, le=24)
    max_rounds: int = Field(default=5, ge=1, le=5)
    max_model_tokens: int = Field(default=12000, ge=1, le=200000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def namespaces(self) -> "PolicyDefinition":
        if "workflow.plan" in self.allowed_tools:
            raise ValueError("Internal planning cannot be granted by a policy")
        if any(not re.fullmatch(NAME_PATTERN, name) for name in self.allowed_namespaces):
            raise ValueError("Invalid policy memory namespace")
        return self


REGISTRY_MODELS = {
    "agents": AgentDefinition, "workflows": WorkflowDefinition, "policies": PolicyDefinition,
}


class MemoryIn(StrictModel):
    content: str = Field(min_length=1, max_length=16000)
    metadata: dict = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=1, le=31536000)
    source_run_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def bounded_metadata(self) -> "MemoryIn":
        if len(json.dumps(self.metadata)) > 4000:
            raise ValueError("Memory metadata exceeds 4000 characters")
        return self


def offline_plan(goal: str) -> list[Step]:
    """A deterministic arithmetic or evidence retrieval/synthesis plan."""
    match = re.fullmatch(
        r"\s*(?:calculate|math|compute)\s*:?\s*([0-9+*/().% \t-]{1,120})\s*",
        goal, flags=re.IGNORECASE,
    )
    if match:
        return [Step(id="calculate", tool="math.calculate", args={"expression": match[1]})]
    return [
        Step(id="retrieve", tool="rag.search", args={"question": goal, "mode": "hybrid"}),
        Step(id="synthesize", tool="model.summarize",
             args={"question": goal}, depends_on=["retrieve"]),
    ]
