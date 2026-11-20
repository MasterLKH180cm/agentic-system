"""Strict, bounded workflow and tool definitions."""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ToolName = Literal[
    "rag.search", "model.summarize", "math.calculate", "data.merge", "artifact.publish"
]


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    tool: ToolName
    args: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    approval_required: bool = False

    @model_validator(mode="after")
    def validate_step(self) -> "Step":
        if len(json.dumps(self.args)) > 8_000:
            raise ValueError("Step arguments exceed 8000 characters")
        if self.tool == "artifact.publish":
            self.approval_required = True
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Duplicate dependencies")
        return self


class WorkflowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=3, max_length=2000)
    steps: list[Step] | None = Field(default=None, max_length=24)
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )

    @model_validator(mode="after")
    def check_workflow(self) -> "WorkflowIn":
        if self.steps is None:
            return self
        if not self.steps:
            raise ValueError("Explicit workflow must contain at least one step")
        names = {s.id for s in self.steps}
        if len(names) != len(self.steps):
            raise ValueError("Duplicate step ID")
        dependencies = {s.id: set(s.depends_on) for s in self.steps}
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
        return self


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    reason: str = Field(default="", max_length=500)


def offline_plan(goal: str) -> list[Step]:
    """Safe deterministic fallback: a useful RAG-and-summary pipeline."""
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
