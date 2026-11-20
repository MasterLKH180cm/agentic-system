"""Checkpointed plan/execute/evaluate/replan loop with explicit stopping reasons."""
import copy
import math
import re

from agentic.control import ControlPlane
from agentic.memory import terms
from agentic.planner import plan
from agentic.schemas import Step
from agentic.schemas import offline_plan
from agentic.store import LeaseLost, Store


CITATION = re.compile(r"\[([^\[\]\s]{1,200})\](?!\()")


def evaluate(goal: str, observations: dict, evaluator: str, memory_evidence: dict | None = None) -> dict:
    """Deterministic evidence checks; lexical support is not a semantic proof."""
    if evaluator == "arithmetic":
        from agentic.tools import safe_calculate
        values = [item["result"].get("value") for item in observations.values()
                  if item["tool"] == "math.calculate"]
        valid = bool(values) and all(type(v) in {int, float} and math.isfinite(v) for v in values)
        requested = offline_plan(goal)
        if requested[0].tool != "math.calculate":
            valid = False
        elif valid:
            expected = safe_calculate(requested[0].args["expression"])
            valid = math.isclose(values[-1], expected, rel_tol=1e-12, abs_tol=1e-12)
        return {"accepted": valid, "reason": "verified_arithmetic" if valid else "arithmetic_mismatch",
                "answer": str(values[-1]) if valid else "", "citations": [],
                "evaluator": "bounded_arithmetic"}
    evidence = {}
    for item in observations.values():
        if item["tool"] in {"rag.search", "memory.search"}:
            for source in item["result"].get("citations", []):
                if isinstance(source, dict) and isinstance(source.get("chunk_id"), str):
                    evidence[source["chunk_id"]] = source
    for source in (memory_evidence or {}).get("citations", []):
        evidence[source["chunk_id"]] = source
    answer = next((item["result"].get("answer", "") for item in reversed(list(observations.values()))
                   if item["result"].get("answer")), "")
    refs = set(CITATION.findall(answer))
    result = {"accepted": False, "answer": answer, "citations": [], "evaluator": "lexical_evidence_v1"}
    if refs - evidence.keys():
        return {**result, "reason": "unknown_citation"}
    sources = [evidence[key] for key in refs if isinstance(evidence[key].get("text"), str)
               and len(evidence[key]["text"].strip()) >= 8 and evidence[key].get("document_id")]
    if not sources:
        return {**result, "reason": "missing_source_evidence"}
    text = "\n".join(source["text"] for source in sources)
    answer_text = CITATION.sub("", answer)
    query_terms = terms(goal)
    answer_terms = terms(answer_text) - {"offline", "evidence", "synthesis", "sources", "retrieved", "extractive", "mode"}
    source_terms = terms(text)
    relevance = len(query_terms & source_terms) / max(1, len(query_terms))
    support = len(answer_terms & source_terms) / max(1, len(answer_terms))
    answer_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", answer_text))
    source_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", text))
    if "insufficient evidence" in answer.lower() or relevance < 0.2 or support < 0.35:
        reason = "insufficient_lexical_support"
    elif answer_numbers - source_numbers:
        reason = "unsupported_numeric_claim"
    else:
        reason = "supported_evidence"
    return {**result, "accepted": reason == "supported_evidence", "reason": reason,
            "citations": sources if reason == "supported_evidence" else [],
            "query_overlap": round(relevance, 4), "answer_overlap": round(support, 4)}


def bounded_result(result: dict) -> dict:
    """Bound persisted observations while preserving source identities and text."""
    safe = copy.deepcopy(result)
    if isinstance(safe.get("answer"), str):
        safe["answer"] = safe["answer"][:8000]
    if isinstance(safe.get("citations"), list):
        safe["citations"] = safe["citations"][:10]
        for source in safe["citations"]:
            if isinstance(source.get("text"), str):
                source["text"] = source["text"][:4000]
    return safe


class AgentRuntime:
    def __init__(self, store: Store, tools):
        self.store, self.tools = store, tools

    def save(self, job: dict, state: dict, event: str, details: dict) -> None:
        if not self.store.checkpoint(job, state, event=event, details=details):
            raise LeaseLost("Runtime lease or workflow is no longer active")

    async def run(self, job: dict) -> dict:
        specification = job["specification"]
        state = copy.deepcopy(job.get("checkpoint") or {})
        if state.get("finished"):
            return state["result"]
        if not state:
            namespace = specification.get("memory_namespace", "default")
            memories = []
            if "memory.search" in specification.get("allowed_tools", []):
                memories = self.tools.memory.search(job["tenant"], namespace, job["goal"], 5)
            state = {"version": 1, "round": 1, "plan": None, "cursor": 0, "observations": {},
                     "evaluations": [], "evaluation": None, "memory": self.tools.memory_evidence(memories)}
            self.save(job, state, "runtime_started", {"max_rounds": specification["max_rounds"]})
        while state["round"] <= specification["max_rounds"]:
            if state["plan"] is None:
                feedback = state["evaluations"][-1] if state["evaluations"] else None
                steps = await plan(
                    job["goal"], self.tools.model, specification=specification, tenant=job["tenant"],
                    run_id=job["run_id"], round_number=state["round"], feedback=feedback,
                    memory=[state["memory"]] if state["memory"]["citations"] else [], loop=True,
                )
                ControlPlane.validate_steps(steps, specification, loop=True)
                state["plan"] = [step.model_dump() for step in steps]
                self.save(job, state, "round_planned", {"round": state["round"], "steps": len(steps)})
            steps = [Step.model_validate(s) for s in state["plan"]]
            ControlPlane.validate_steps(steps, specification, loop=True)
            while len(state["observations"]) < len(steps):
                step = next((s for s in steps if s.id not in state["observations"]
                             and all(dep in state["observations"] for dep in s.depends_on)), None)
                if step is None:
                    raise ValueError("Checkpoint plan cannot make progress")
                inputs = {dep: state["observations"][dep]["result"] for dep in step.depends_on}
                if step.tool == "model.summarize" and state["memory"]["citations"]:
                    inputs["recalled_memory"] = state["memory"]
                nested_job = {**job, "id": f"round{state['round']}:{step.id}", "tool": step.tool,
                              "args": step.args, "approved": False, "in_agent_loop": True}
                result = await self.tools.execute(nested_job, inputs)
                state["observations"][step.id] = {"tool": step.tool, "result": bounded_result(result)}
                state["cursor"] = len(state["observations"])
                self.save(job, state, "tool_observed", {"round": state["round"], "tool": step.tool,
                                                       "cursor": state["cursor"]})
            assessment = state.get("evaluation")
            if assessment is None:
                assessment = evaluate(job["goal"], state["observations"], specification["evaluator"], state["memory"])
                summary = {key: value for key, value in assessment.items() if key not in {"answer", "citations"}}
                state["evaluations"].append({"round": state["round"], **summary})
                state["evaluation"] = assessment
                self.save(job, state, "round_evaluated", {"round": state["round"], **summary})
            if assessment["accepted"]:
                result = {"outcome": "success", "stop_reason": assessment["reason"],
                          "answer": assessment["answer"], "citations": assessment["citations"],
                          "rounds": state["round"], "evaluations": state["evaluations"]}
                state.update(finished=True, result=result)
                self.save(job, state, "runtime_finished", {"outcome": "success", "rounds": state["round"]})
                return result
            state.update(round=state["round"] + 1, plan=None, cursor=0, observations={}, evaluation=None)
            self.save(job, state, "runtime_replan", {"round": state["round"], "reason": assessment["reason"]})
        result = {"outcome": "insufficient_evidence", "stop_reason": "rounds_exhausted",
                  "answer": "Insufficient evidence after the bounded refinement rounds.", "citations": [],
                  "rounds": specification["max_rounds"], "evaluations": state["evaluations"]}
        state.update(finished=True, result=result)
        self.save(job, state, "runtime_finished", {"outcome": "insufficient_evidence"})
        return result
