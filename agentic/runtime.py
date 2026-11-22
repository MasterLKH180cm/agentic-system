"""Checkpointed plan/execute/evaluate/replan loop with explicit stopping reasons."""
import copy
import math
import re

from agentic.control import ControlPlane
from agentic.admission import CONTRACT_VERSION, REFUSAL_REASONS, safe_metadata
from agentic.evidence import CITATION, collect_evidence, is_refused
from agentic.memory import terms
from agentic.planner import plan
from agentic.schemas import Step
from agentic.schemas import offline_plan
from agentic.store import LeaseLost, Store


def evaluate(goal: str, observations: dict, evaluator: str, memory_evidence: dict | None = None,
             *, policy: str = "compatibility", profiles: list | tuple = ()) -> dict:
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
    original = {name: item["result"] for name, item in observations.items()
                if item["tool"] in {"rag.search", "memory.search", "math.calculate", "data.merge"}}
    original.update({name: item["result"] for name, item in observations.items()
                     if item["tool"] == "model.summarize" and is_refused(item["result"])})
    if memory_evidence:
        original["__recalled_memory"] = memory_evidence
    # Summary excerpts may be shorter than original sources. Evaluate against
    # original evidence, preserving admission and source identity independently.
    bundle = collect_evidence(original, policy=policy, profiles=profiles)
    final = next((item["result"] for item in reversed(list(observations.values()))
                  if item["tool"] == "model.summarize"), None)
    if final is None:
        final = next((item["result"] for item in reversed(list(observations.values()))
                      if item["result"].get("answer")), {})
    answer = final.get("answer") or ""
    refs = set(CITATION.findall(answer))
    result = {"accepted": False, "answer": answer, "citations": [], "evaluator": "admitted_evidence_v2"}
    if is_refused(final):
        reason = final.get("refusal_reason") or final.get("retrieval", {}).get("refusal_reason")
        return {**result, "reason": "evidence_refused",
                "refusal_reason": reason if reason in REFUSAL_REASONS else bundle.refusal_reason}
    if refs - bundle.sources.keys():
        return {**result, "reason": "unknown_citation"}
    sources = [bundle.sources[key] for key in refs if bundle.sources[key]["text"].strip()]
    answer_text = CITATION.sub("", answer)
    answer_numbers = set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", answer_text))
    observation_text = "\n".join(str(item["value"]) + " " + item.get("expression", "") + " " + item["step"]
                                 for item in bundle.observations)
    exact_numbers = set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", observation_text))
    if not sources:
        if bundle.observations and answer_numbers and not answer_numbers - exact_numbers:
            return {**result, "accepted": True, "reason": "verified_observation",
                    "observations": bundle.observations}
        return {**result, "reason": "missing_source_evidence", "refusal_reason": bundle.refusal_reason}
    text = "\n".join(source["text"] for source in sources)
    query_terms = terms(goal)
    answer_terms = terms(answer_text) - {"offline", "evidence", "synthesis", "sources", "retrieved", "extractive", "mode"}
    source_terms = terms(text)
    relevance = len(query_terms & source_terms) / max(1, len(query_terms))
    support = len(answer_terms & source_terms) / max(1, len(answer_terms))
    source_numbers = set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)) | exact_numbers
    calibrated = all(bundle.admissions.get(source["chunk_id"], {}).get("admission", {}).get("basis")
                     in {"calibrated_reranker", "calibrated_answerability"}
                     for source in sources)
    if (not calibrated and relevance < 0.2) or support < 0.35:
        reason = "insufficient_lexical_support"
    elif answer_numbers - source_numbers:
        reason = "unsupported_numeric_claim"
    else:
        reason = "supported_evidence"
    return {**result, "accepted": reason == "supported_evidence", "reason": reason,
            "citations": sources if reason == "supported_evidence" else [],
            "query_overlap": round(relevance, 4), "answer_overlap": round(support, 4),
            "calibrated_relevance": calibrated,
            "refusal_reason": None if reason == "supported_evidence" else "model_rejected"}


def bounded_result(result: dict) -> dict:
    """Tool-level caps already apply; do not sever citations from their metadata."""
    return copy.deepcopy(result)


class AgentRuntime:
    def __init__(self, store: Store, tools):
        self.store, self.tools = store, tools

    def save(self, job: dict, state: dict, event: str, details: dict) -> None:
        if not self.store.checkpoint(job, state, event=event, details=details):
            raise LeaseLost("Runtime lease or workflow is no longer active")

    async def run(self, job: dict) -> dict:
        specification = job["specification"]
        snapshot = specification.get("feature_flags")
        features = self.tools.config.features
        features.require("agent_loop", snapshot)
        policy, profiles = self.tools.evidence_policy(job)
        state = copy.deepcopy(job.get("checkpoint") or {})
        if state.get("memory", {}).get("citations"):
            features.require("memory", snapshot)
        if state.get("plan"):
            ControlPlane.validate_steps([Step.model_validate(step) for step in state["plan"]], specification,
                                        loop=True, features=features, online=self.tools.config.provider != "offline")
        if state and state.get("version", 1) < CONTRACT_VERSION:
            state.update(version=CONTRACT_VERSION, evaluation=None)
            # Reassess existing observations without replaying their tool calls.
            if state.get("result", {}).get("outcome") == "success":
                state["finished"] = False
        if state.get("finished"):
            return state["result"]
        if not state:
            namespace = specification.get("memory_namespace", "default")
            memories = []
            if "memory.search" in specification.get("allowed_tools", []):
                features.require("memory", snapshot)
                memories = self.tools.memory.search(job["tenant"], namespace, job["goal"], 5)
            state = {"version": CONTRACT_VERSION, "round": 1, "plan": None, "cursor": 0, "observations": {},
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
                ControlPlane.validate_steps(steps, specification, loop=True, features=features,
                                            online=self.tools.config.provider != "offline")
                state["plan"] = [step.model_dump() for step in steps]
                self.save(job, state, "round_planned", {"round": state["round"], "steps": len(steps)})
            steps = [Step.model_validate(s) for s in state["plan"]]
            ControlPlane.validate_steps(steps, specification, loop=True, features=features,
                                        online=self.tools.config.provider != "offline")
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
                                                       "cursor": state["cursor"], **safe_metadata(result, profiles)})
            assessment = state.get("evaluation")
            if assessment is None:
                assessment = evaluate(job["goal"], state["observations"], specification["evaluator"], state["memory"],
                                      policy=policy, profiles=profiles)
                summary = {key: value for key, value in assessment.items() if key not in {"answer", "citations", "observations"}}
                state["evaluations"].append({"round": state["round"], **summary})
                state["evaluation"] = assessment
                self.save(job, state, "round_evaluated", {"round": state["round"], **summary})
            if assessment["accepted"]:
                result = {"outcome": "success", "stop_reason": assessment["reason"],
                          "answer": assessment["answer"], "citations": assessment["citations"],
                          "observations": assessment.get("observations", []),
                          "rounds": state["round"], "evaluations": state["evaluations"],
                          "evidence_contract_version": CONTRACT_VERSION}
                state.update(finished=True, result=result)
                self.save(job, state, "runtime_finished", {"outcome": "success", "rounds": state["round"]})
                return result
            state["last_refusal_reason"] = assessment.get("refusal_reason") or "model_rejected"
            state.update(round=state["round"] + 1, plan=None, cursor=0, observations={}, evaluation=None)
            self.save(job, state, "runtime_replan", {"round": state["round"], "reason": assessment["reason"]})
        result = {"outcome": "insufficient_evidence", "stop_reason": "rounds_exhausted",
                  "answer": "Insufficient evidence after the bounded refinement rounds.", "citations": [],
                  "rounds": specification["max_rounds"], "evaluations": state["evaluations"],
                  "last_refusal_reason": state.get("last_refusal_reason", "no_candidates"),
                  "evidence_contract_version": CONTRACT_VERSION}
        state.update(finished=True, result=result)
        self.save(job, state, "runtime_finished", {"outcome": "insufficient_evidence", **safe_metadata(result, profiles)})
        return result
