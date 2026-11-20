"""Bounded planning: model proposes only schema-validated, allowlisted DAG steps."""
import json

from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn, offline_plan


SYSTEM = """Design a bounded workflow as a JSON object:
{"steps":[{"id":"retrieve","tool":"rag.search","args":{"question":"..."}},
{"id":"synthesize","tool":"model.summarize","depends_on":["retrieve"]}]}
Allowed tools: rag.search, model.summarize, math.calculate, data.merge,
artifact.publish. Model suggestions cannot execute arbitrary code, SQL,
webhooks or shell commands. artifact.publish ALWAYS requires human approval.
Use <= 8 steps. Use depends_on to order dependent steps and allow independent
steps to run in parallel. Return ONLY JSON, not markdown."""


async def plan(goal: str, model: ModelGateway) -> list[Step]:
    if model.config.provider == "offline":
        return offline_plan(goal)
    response = await model.complete(SYSTEM, goal)
    if not response:
        raise ValueError("Planner returned no valid output")
    try:
        parsed = json.loads(response)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
            raise ValueError("Planner output must contain a steps array")
        if not 1 <= len(parsed["steps"]) <= 8:
            raise ValueError("Planner must create 1 to 8 steps")
        data = WorkflowIn(goal=goal, steps=[Step.model_validate(s) for s in parsed["steps"]])
        assert data.steps is not None
        return data.steps
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Planner returned invalid or unsafe workflow JSON") from exc
