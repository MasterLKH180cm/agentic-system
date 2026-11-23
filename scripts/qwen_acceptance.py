"""Pure checks for the fixed public Qwen workflow smoke cases, not a quality judge."""
import re

from agentic.evidence import CITATION, SOURCE_ID
from agentic.schemas import Step, validate_dag

ROUTE = "qwen-gateway-v1"
ROUTER_MODEL = "router:gateway/qwen3-1.7b-q8_0-v1"
ARCHIVE_TEXT = (
    "The sample archive retains exports for 14 days. "
    "After 14 days, sample exports are deleted."
)
CASES = {"explicit_arithmetic", "planned_retrieval", "adaptive_retrieval"}


class _Rejected(Exception):
    pass


def _require(condition, code):
    if not condition:
        raise _Rejected(code)


def _usage(state, case):
    usage = state["usage"]
    calls = usage["calls"]
    _require(isinstance(calls, list) and bool(calls), "qwen_usage_missing")
    _require(usage["usage_complete"] is True, "qwen_usage_incomplete")
    identifiers, purposes = set(), {"planning": 0, "synthesis": 0}
    totals = {"input_tokens": 0, "output_tokens": 0, "charged_tokens": 0}
    for call in calls:
        _require(isinstance(call["id"], str) and bool(call["id"]) and call["id"] not in identifiers,
                 "qwen_usage_identity_invalid")
        identifiers.add(call["id"])
        _require(call["tenant"] == state["tenant"] and call["run_id"] == state["id"]
                 and call["route"] == ROUTE, "qwen_usage_identity_invalid")
        _require(call["status"] == "reported" and call["purpose"] in purposes, "qwen_usage_not_reported")
        _require(all(type(call[key]) is int and 0 < call[key] <= 2_000_000_000
                     for key in ("input_tokens", "output_tokens")), "qwen_usage_tokens_invalid")
        _require(type(call["charged_tokens"]) is int
                 and call["charged_tokens"] == call["input_tokens"] + call["output_tokens"],
                 "qwen_usage_charge_invalid")
        purposes[call["purpose"]] += 1
        for key in totals:
            totals[key] += call[key]
    _require(type(usage["charged_tokens"]) is int and usage["charged_tokens"] == totals["charged_tokens"],
             "qwen_usage_total_invalid")
    _require(purposes["synthesis"] > 0 and (purposes["planning"] > 0 if case != "explicit_arithmetic"
                                            else purposes["planning"] == 0), "qwen_model_purpose_missing")
    return purposes, totals


def _graph(nodes, allowed):
    _require(isinstance(nodes, list) and bool(nodes), "qwen_plan_missing")
    _require(all(node["tool"] in allowed and node["status"] == "completed" for node in nodes),
             "qwen_tool_not_completed")
    steps = [Step.model_validate({key: node.get(key, {} if key == "args" else [])
                                  for key in ("id", "tool", "args", "depends_on")}) for node in nodes]
    validate_dag(steps)
    return {node["id"]: node for node in nodes}


def _loop_nodes(state):
    loops = state["steps"]
    _require(isinstance(loops, list) and len(loops) == 1 and loops[0]["tool"] == "agent.run"
             and loops[0]["status"] == "completed", "qwen_loop_not_completed")
    loop = loops[0]
    checkpoint = loop["checkpoint"]
    terminal = loop["result"]
    evaluation = checkpoint["evaluation"]
    _require(checkpoint["finished"] is True and evaluation["accepted"] is True, "qwen_loop_not_accepted")
    _require(terminal == checkpoint["result"] and terminal["outcome"] == "success"
             and terminal["rounds"] == checkpoint["round"]
             and terminal["answer"] == evaluation["answer"]
             and terminal["citations"] == evaluation["citations"], "qwen_loop_result_mismatch")
    plan = checkpoint["plan"]
    _require(isinstance(plan, list) and 1 <= len(plan) <= 8, "qwen_loop_plan_invalid")
    by_id = {step["id"]: step for step in plan}
    observations = checkpoint["observations"]
    _require(len(by_id) == len(plan) and set(observations) == set(by_id)
             and checkpoint["cursor"] == len(plan), "qwen_loop_observations_incomplete")
    # Dict insertion order is the actual persisted tool execution order. The
    # planner may legally return a DAG whose declaration order is different.
    nodes, observed = [], set()
    for identifier, observation in observations.items():
        step = by_id[identifier]
        _require(observation["tool"] == step["tool"]
                 and set(step.get("depends_on", [])) <= observed, "qwen_loop_observation_mismatch")
        nodes.append({**step, "status": "completed", "result": observation["result"]})
        observed.add(identifier)
    return nodes, terminal


def _answer(result, arithmetic):
    _require(result["outcome"] == "success", "qwen_synthesis_not_successful")
    answer = result["answer"]
    _require(isinstance(answer, str) and bool(answer.strip()), "qwen_answer_invalid")
    plain = CITATION.sub("", answer)
    numbers = set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", plain))
    expected, supported = ("437", {"19", "23", "437"}) if arithmetic else ("14", {"14"})
    _require(expected in numbers and numbers <= supported, "qwen_answer_invalid")
    if not arithmetic:
        _require(re.search(r"\b14(?:\s+|-)(?:calendar\s+)?days?\b", plain, re.IGNORECASE),
                 "qwen_answer_invalid")
    return set(CITATION.findall(answer))


def _sources(citations):
    _require(isinstance(citations, list) and bool(citations), "qwen_citations_missing")
    sources = {}
    for citation in citations:
        identifier = citation["chunk_id"]
        _require(isinstance(identifier, str) and SOURCE_ID.fullmatch(identifier)
                 and identifier not in sources and citation["document_id"] == "sample-archive"
                 and citation["text"] == ARCHIVE_TEXT, "qwen_citation_identity_invalid")
        # Search scores can differ across queries; the source identity cannot.
        sources[identifier] = {"document_id": citation["document_id"], "text": citation["text"]}
    return sources


def _retrieval_summary(node, graph):
    result = node["result"]
    references = _answer(result, False)
    cited = _sources(result["citations"])
    retrieved = {}
    for dependency in node["depends_on"]:
        source = graph[dependency]
        if source["tool"] == "rag.search":
            evidence = source["result"]
            _require(evidence["retrieval"]["outcome"] == "success", "qwen_retrieval_not_accepted")
            for identifier, citation in _sources(evidence["citations"]).items():
                _require(identifier not in retrieved or retrieved[identifier] == citation,
                         "qwen_citation_identity_invalid")
                retrieved[identifier] = citation
    _require(bool(retrieved), "qwen_synthesis_dependency_missing")
    _require(bool(references) and references == set(cited) and references <= set(retrieved),
             "qwen_citation_reference_invalid")
    _require(all(cited[key]["text"] == retrieved[key]["text"]
                 and cited[key]["document_id"] == retrieved[key]["document_id"] for key in cited),
             "qwen_citation_identity_invalid")
    return len(cited)


def _arithmetic_summary(node, graph):
    result = node["result"]
    _require(not _answer(result, True) and result["citations"] == [], "qwen_arithmetic_citation_invalid")
    calculations = {identifier for identifier in node["depends_on"]
                    if graph[identifier]["tool"] == "math.calculate"
                    and type(graph[identifier]["result"].get("value")) in (int, float)
                    and graph[identifier]["result"]["value"] == 437
                    and graph[identifier]["args"]["expression"].replace(" ", "") == "19*23"}
    _require(bool(calculations), "qwen_synthesis_dependency_missing")
    _require(any(observation.get("step") in calculations and observation.get("kind") == "arithmetic"
                 and type(observation.get("value")) in (int, float) and observation["value"] == 437
                 for observation in result["observations"]), "qwen_arithmetic_observation_missing")
    return 0


def validate_rag_generation(answer):
    """Validate this fixture's RAG answer, exact citations and actual generation usage."""
    try:
        _require(answer["outcome"] == "success", "qwen_rag_generation_failed")
        references = _answer(answer, False)
        sources = _sources(answer["citations"])
        _require(bool(references) and references == set(sources), "qwen_citation_reference_invalid")
        usage = answer.get("usage")
        _require(isinstance(usage, dict) and usage.get("token_usage") == "provider_reported",
                 "qwen_rag_usage_unreported")
        reports = usage.get("provider_reports")
        _require(isinstance(reports, list) and bool(reports), "qwen_rag_generation_usage_missing")
        _require(type(usage.get("calls")) is int and usage["calls"] >= len(reports),
                 "qwen_rag_usage_count_invalid")
        generation = [item for item in reports if item.get("kind") == "generation"
                      and item.get("model") == ROUTER_MODEL]
        _require(bool(generation), "qwen_rag_generation_usage_missing")
        totals = {"prompt_tokens": 0, "completion_tokens": 0}
        for item in generation:
            counters = item["usage"]
            _require(all(type(counters.get(field)) is int and 0 < counters[field] <= 2_000_000_000
                         for field in totals), "qwen_rag_usage_tokens_invalid")
            for field in totals:
                totals[field] += counters[field]
        return {"schema_version": "qwen-rag-proof-v1", "status": "passed", "expected_value": 14,
                "citation_count": len(sources), "cited_document_ids": ["sample-archive"],
                "model": ROUTER_MODEL, "reported_generation_calls": len(generation), "usage": totals}
    except _Rejected as exc:
        raise ValueError(str(exc)) from None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        raise ValueError("qwen_acceptance_state_invalid") from None


def validate_workflow(state, case):
    """Return public counters or a fixed ValueError code; never echo model text."""
    try:
        _require(isinstance(case, str) and case in CASES, "qwen_case_unknown")
        _require(state["status"] == "completed", "qwen_workflow_not_completed")
        _require(all(isinstance(state[key], str) and bool(state[key]) for key in ("tenant", "id")),
                 "qwen_workflow_identity_invalid")
        purposes, totals = _usage(state, case)
        arithmetic = case == "explicit_arithmetic"
        nodes, terminal = _loop_nodes(state) if case == "adaptive_retrieval" else (state["steps"], None)
        allowed = {"math.calculate", "model.summarize"} if arithmetic else {"rag.search", "model.summarize"}
        if case == "planned_retrieval":
            allowed.add("workflow.plan")
        graph = _graph(nodes, allowed)
        if case == "planned_retrieval":
            planners = [node for node in nodes if node["tool"] == "workflow.plan"]
            _require(len(planners) == 1, "qwen_completed_planning_missing")
            planner = planners[0]
            installed = set(graph) - {planner["id"]}
            _require(planner["checkpoint"]["expanded"] is True
                     and set(planner["checkpoint"]["step_ids"]) == installed
                     and set(planner["result"]["planned_steps"]) == installed
                     and all(planner["id"] in graph[key]["depends_on"] for key in installed),
                     "qwen_planning_installation_mismatch")
        summaries = [node for node in nodes if node["tool"] == "model.summarize"]
        _require(bool(summaries), "qwen_completed_synthesis_missing")
        _require(purposes["synthesis"] >= len(summaries), "qwen_synthesis_usage_missing")
        citations = [_arithmetic_summary(node, graph) if arithmetic else _retrieval_summary(node, graph)
                     for node in summaries]
        if terminal is not None:
            final = summaries[-1]["result"]
            _require(terminal["answer"] == final["answer"]
                     and _sources(terminal["citations"]) == _sources(final["citations"]),
                     "qwen_loop_final_synthesis_mismatch")
        return {"schema_version": "qwen-workflow-proof-v1", "case": case, "status": "passed",
                "expected_value": 437 if arithmetic else 14, "completed_syntheses": len(summaries),
                "completed_retrievals": sum(node["tool"] == "rag.search" for node in nodes),
                "citation_count": citations[-1], "cited_document_ids": [] if arithmetic else ["sample-archive"],
                "reported_calls_by_purpose": purposes, "usage": totals}
    except _Rejected as exc:
        raise ValueError(str(exc)) from None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        raise ValueError("qwen_acceptance_state_invalid") from None
