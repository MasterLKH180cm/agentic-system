"""Acceptance cannot mistake retrieved text or a usage row for real synthesis."""
import json
from copy import deepcopy

import pytest

from scripts.qwen_acceptance import ARCHIVE_TEXT, validate_rag_generation, validate_workflow

pytestmark = pytest.mark.unit


def source():
    return {"chunk_id": "archive:chunk-1", "document_id": "sample-archive", "text": ARCHIVE_TEXT}


def summary():
    return {"outcome": "success", "answer": "Sample archive exports are retained for 14 days [archive:chunk-1].",
            "citations": [source()], "observations": [], "refusal_reason": None}


def usage(purposes):
    calls = [{"id": f"call-{index}", "tenant": "qwen-live-synthetic", "run_id": "run-qwen",
              "route": "qwen-gateway-v1", "purpose": purpose, "status": "reported",
              "input_tokens": 30, "output_tokens": 20, "charged_tokens": 50}
             for index, purpose in enumerate(purposes)]
    return {"calls": calls, "charged_tokens": sum(call["charged_tokens"] for call in calls),
            "usage_complete": True, "estimated_cost_usd": None, "cost_complete": False}


def completed(identifier, tool, result, dependencies=(), args=None):
    return {"id": identifier, "tool": tool, "args": args or {}, "depends_on": list(dependencies),
            "status": "completed", "result": result, "checkpoint": {}}


def valid_state(case):
    state = {"id": "run-qwen", "tenant": "qwen-live-synthetic", "status": "completed"}
    if case == "explicit_arithmetic":
        state["steps"] = [
            completed("calculate", "math.calculate", {"expression": "19 * 23", "value": 437},
                      args={"expression": "19 * 23"}),
            completed("answer", "model.summarize", {"outcome": "success", "answer": "19 * 23 = 437.",
                      "citations": [], "observations": [{"step": "calculate", "kind": "arithmetic",
                                                         "expression": "19 * 23", "value": 437}]}, ["calculate"]),
        ]
        state["usage"] = usage(["synthesis"])
        return state
    retrieval = completed("retrieve", "rag.search", {"retrieval": {"outcome": "success"},
                          "answer": "14 days [archive:chunk-1]", "citations": [source()]},
                          args={"question": "Sample retention period", "top_k": 2})
    synthesis = completed("answer", "model.summarize", summary(), ["retrieve"])
    state["usage"] = usage(["planning", "synthesis"])
    if case == "planned_retrieval":
        planner = completed("planner", "workflow.plan", {"planned_steps": ["retrieve", "answer"]})
        planner["checkpoint"] = {"expanded": True, "step_ids": ["retrieve", "answer"]}
        retrieval["depends_on"].append("planner")
        synthesis["depends_on"].append("planner")
        state["steps"] = [planner, retrieval, synthesis]
        return state
    terminal = {"outcome": "success", "answer": synthesis["result"]["answer"],
                "citations": deepcopy(synthesis["result"]["citations"]), "rounds": 1}
    loop = completed("agent", "agent.run", terminal)
    loop["checkpoint"] = {
        "finished": True, "round": 1, "cursor": 2,
        # A legal declaration order need not be execution order.
        "plan": [{key: step[key] for key in ("id", "tool", "args", "depends_on")}
                 for step in (synthesis, retrieval)],
        "observations": {step["id"]: {"tool": step["tool"], "result": deepcopy(step["result"])}
                         for step in (retrieval, synthesis)},
        "evaluation": {"accepted": True, "answer": terminal["answer"], "citations": deepcopy(terminal["citations"])},
        "result": deepcopy(terminal),
    }
    state["steps"] = [loop]
    return state


@pytest.mark.parametrize("case", ["explicit_arithmetic", "planned_retrieval", "adaptive_retrieval"])
def test_actual_contract_shapes_produce_only_public_counter_summaries(case):
    state = valid_state(case)
    state["private_marker"] = "confidential-request-content"
    original = deepcopy(state)
    result = validate_workflow(state, case)
    assert result["status"] == "passed" and result["completed_syntheses"] == 1
    assert result["expected_value"] == (437 if case == "explicit_arithmetic" else 14)
    assert result["reported_calls_by_purpose"] == {
        "planning": 0 if case == "explicit_arithmetic" else 1, "synthesis": 1,
    }
    assert result["usage"]["charged_tokens"] == (50 if case == "explicit_arithmetic" else 100)
    assert not any(value in json.dumps(result) for value in
                   ("confidential-request-content", "run-qwen", "archive:chunk-1", ARCHIVE_TEXT))
    assert state == original


@pytest.mark.parametrize("case", ["planned_retrieval", "adaptive_retrieval"])
def test_planning_only_usage_does_not_prove_synthesis(case):
    state = valid_state(case)
    state["usage"] = usage(["planning"])
    with pytest.raises(ValueError, match="^qwen_model_purpose_missing$"):
        validate_workflow(state, case)


def test_retrieval_only_dag_cannot_pass_from_source_text_or_a_forged_synthesis_usage_row():
    state = valid_state("planned_retrieval")
    state["steps"].pop()
    state["steps"][0]["result"]["planned_steps"] = ["retrieve"]
    state["steps"][0]["checkpoint"]["step_ids"] = ["retrieve"]
    assert "14" in json.dumps(state["steps"]) and "sample-archive" in json.dumps(state["steps"])
    with pytest.raises(ValueError, match="^qwen_completed_synthesis_missing$"):
        validate_workflow(state, "planned_retrieval")


def test_retrieval_only_accepted_loop_does_not_prove_synthesis():
    state = valid_state("adaptive_retrieval")
    loop = state["steps"][0]
    cp = loop["checkpoint"]
    cp["plan"] = [step for step in cp["plan"] if step["tool"] == "rag.search"]
    cp["observations"].pop("answer")
    cp["cursor"] = 1
    # The runtime's evidence evaluator can accept a retrieval-only answer;
    # acceptance must still prove that this workload actually used synthesis.
    for result in (loop["result"], cp["result"], cp["evaluation"]):
        result["answer"] = cp["observations"]["retrieve"]["result"]["answer"]
    with pytest.raises(ValueError, match="^qwen_completed_synthesis_missing$"):
        validate_workflow(state, "adaptive_retrieval")


def test_loop_final_answer_uses_observed_synthesis_order_not_plan_declaration_order():
    state = valid_state("adaptive_retrieval")
    loop = state["steps"][0]
    cp = loop["checkpoint"]
    final = {**summary(), "answer": "Exports are deleted after 14 days [archive:chunk-1]."}
    cp["plan"].insert(0, {"id": "final", "tool": "model.summarize", "args": {},
                           "depends_on": ["retrieve", "answer"]})
    cp["observations"]["final"] = {"tool": "model.summarize", "result": final}
    cp["cursor"] = 3
    for result in (loop["result"], cp["result"], cp["evaluation"]):
        result["answer"] = final["answer"]
    state["usage"] = usage(["planning", "synthesis", "synthesis"])
    proof = validate_workflow(state, "adaptive_retrieval")
    assert proof["completed_syntheses"] == 2 and proof["reported_calls_by_purpose"]["synthesis"] == 2


def test_retrieval_score_changes_do_not_change_source_identity():
    state = valid_state("planned_retrieval")
    second = deepcopy(state["steps"][1])
    second["id"] = "retrieve_again"
    second["result"]["citations"][0]["score"] = 0.1
    state["steps"][1]["result"]["citations"][0]["score"] = 0.9
    state["steps"].insert(2, second)
    state["steps"][-1]["depends_on"].append("retrieve_again")
    state["steps"][0]["checkpoint"]["step_ids"].append("retrieve_again")
    state["steps"][0]["result"]["planned_steps"].append("retrieve_again")
    proof = validate_workflow(state, "planned_retrieval")
    assert proof["completed_retrievals"] == 2 and proof["citation_count"] == 1


@pytest.mark.parametrize("answer", [
    "The result is 438.", "The result is 4370.", "The result is 438, not 437.", "Result [437].",
])
def test_correct_calculation_elsewhere_does_not_hide_wrong_synthesis(answer):
    state = valid_state("explicit_arithmetic")
    state["steps"][1]["result"]["answer"] = answer
    with pytest.raises(ValueError, match="^qwen_answer_invalid$"):
        validate_workflow(state, "explicit_arithmetic")


@pytest.mark.parametrize("answer", [
    "Retained for 140 days [archive:chunk-1].", "Retained for 14 hours [archive:chunk-1].",
    "Retained for 14 days or 140 days [archive:chunk-1].", "No duration [14-days].",
])
def test_retention_value_must_be_in_the_answer_with_correct_unit(answer):
    state = valid_state("planned_retrieval")
    state["steps"][-1]["result"]["answer"] = answer
    with pytest.raises(ValueError, match="^qwen_answer_invalid$"):
        validate_workflow(state, "planned_retrieval")


@pytest.mark.parametrize("fault", ["invented_chunk", "foreign_document", "changed_text", "missing_reference"])
def test_synthesis_citations_must_match_actual_dependency_evidence(fault):
    state = valid_state("planned_retrieval")
    result = state["steps"][-1]["result"]
    if fault == "invented_chunk":
        result["citations"][0]["chunk_id"] = "invented"
        result["answer"] = "Retained for 14 days [invented]."
    elif fault == "foreign_document":
        result["citations"][0]["document_id"] = "foreign"
    elif fault == "changed_text":
        result["citations"][0]["text"] = "An invented claim about 14 days."
    else:
        result["answer"] = "Retained for 14 days."
    with pytest.raises(ValueError, match="^qwen_citation_(identity|reference)_invalid$"):
        validate_workflow(state, "planned_retrieval")


@pytest.mark.parametrize("case", ["planned_retrieval", "adaptive_retrieval"])
def test_summary_must_depend_on_retrieval(case):
    state = valid_state(case)
    if case == "planned_retrieval":
        state["steps"][-1]["depends_on"] = ["planner"]
    else:
        state["steps"][0]["checkpoint"]["plan"][0]["depends_on"] = []
    with pytest.raises(ValueError, match="^qwen_synthesis_dependency_missing$"):
        validate_workflow(state, case)


@pytest.mark.parametrize("field,value,code", [
    ("route", "unapproved", "identity_invalid"), ("tenant", "foreign", "identity_invalid"),
    ("run_id", "foreign", "identity_invalid"), ("status", "reserved", "not_reported"),
    ("purpose", "evaluation", "not_reported"), ("input_tokens", True, "tokens_invalid"),
    ("input_tokens", 0, "tokens_invalid"), ("output_tokens", 0, "tokens_invalid"),
    ("output_tokens", None, "tokens_invalid"), ("charged_tokens", 999, "charge_invalid"),
])
def test_usage_proves_reported_generation_in_the_same_run_and_route(field, value, code):
    state = valid_state("planned_retrieval")
    state["usage"]["calls"][0][field] = value
    with pytest.raises(ValueError, match="^qwen_usage_" + code + "$"):
        validate_workflow(state, "planned_retrieval")


def test_aggregate_charge_and_unique_usage_records_are_required():
    state = valid_state("planned_retrieval")
    state["usage"]["charged_tokens"] = 1
    with pytest.raises(ValueError, match="^qwen_usage_total_invalid$"):
        validate_workflow(state, "planned_retrieval")
    state = valid_state("planned_retrieval")
    state["usage"]["calls"][1]["id"] = state["usage"]["calls"][0]["id"]
    with pytest.raises(ValueError, match="^qwen_usage_identity_invalid$"):
        validate_workflow(state, "planned_retrieval")


@pytest.mark.parametrize("fault,code", [
    ("unfinished", "not_accepted"), ("unaccepted", "not_accepted"),
    ("stale_result", "result_mismatch"), ("stale_round", "result_mismatch"),
    ("missing_observation", "observations_incomplete"), ("tool_mismatch", "observation_mismatch"),
    ("dependency_order", "observation_mismatch"), ("final_differs_from_summary", "final_synthesis_mismatch"),
])
def test_loop_checkpoint_must_show_completed_observed_synthesis_and_matching_final_result(fault, code):
    state = valid_state("adaptive_retrieval")
    loop = state["steps"][0]
    cp = loop["checkpoint"]
    if fault == "unfinished":
        cp["finished"] = False
    elif fault == "unaccepted":
        cp["evaluation"]["accepted"] = False
    elif fault == "stale_result":
        cp["result"]["answer"] = "A different result"
    elif fault == "stale_round":
        cp["round"] = 2
    elif fault == "missing_observation":
        cp["observations"].pop("answer")
    elif fault == "tool_mismatch":
        cp["observations"]["answer"]["tool"] = "rag.search"
    elif fault == "dependency_order":
        cp["observations"] = dict(reversed(list(cp["observations"].items())))
    else:
        for result in (loop["result"], cp["result"], cp["evaluation"]):
            result["answer"] = "Different final text with 14 days [archive:chunk-1]."
    with pytest.raises(ValueError, match="^qwen_loop_" + code + "$"):
        validate_workflow(state, "adaptive_retrieval")


def test_planning_usage_without_a_completed_installed_plan_is_insufficient():
    state = valid_state("planned_retrieval")
    state["steps"][0]["checkpoint"]["expanded"] = False
    with pytest.raises(ValueError, match="^qwen_planning_installation_mismatch$"):
        validate_workflow(state, "planned_retrieval")


@pytest.mark.parametrize("state,case", [(None, "planned_retrieval"), ({"private": "secret-content"}, "planned_retrieval"),
                                      ({}, "secret-content"), ({}, ["secret-content"])])
def test_malformed_state_errors_are_fixed_codes_and_do_not_echo_content(state, case):
    with pytest.raises(ValueError, match=r"^qwen_(acceptance_state_invalid|case_unknown)$") as error:
        validate_workflow(state, case)
    assert "secret-content" not in str(error.value)


def rag_answer():
    return {**summary(), "usage": {"calls": 1, "token_usage": "provider_reported", "provider_reports": [
        {"kind": "generation", "model": "router:gateway/qwen3-1.7b-q8_0-v1",
         "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}},
    ]}}


def test_rag_generation_returns_only_public_verified_counters():
    answer = rag_answer()
    answer["private_marker"] = "confidential-content"
    original = deepcopy(answer)
    result = validate_rag_generation(answer)
    assert result == {
        "schema_version": "qwen-rag-proof-v1", "status": "passed", "expected_value": 14,
        "citation_count": 1, "cited_document_ids": ["sample-archive"],
        "model": "router:gateway/qwen3-1.7b-q8_0-v1", "reported_generation_calls": 1,
        "usage": {"prompt_tokens": 30, "completion_tokens": 20},
    }
    assert not any(value in json.dumps(result) for value in
                   ("confidential-content", "archive:chunk-1", ARCHIVE_TEXT))
    assert answer == original


@pytest.mark.parametrize("text", [
    "Retained for 140 days [archive:chunk-1].", "Retained for 14 hours [archive:chunk-1].",
    "Retained for 14 days or 140 days [archive:chunk-1].",
])
def test_rag_generation_rejects_wrong_values_despite_the_substring_14(text):
    answer = rag_answer()
    answer["answer"] = text
    with pytest.raises(ValueError, match="^qwen_answer_invalid$"):
        validate_rag_generation(answer)


def test_rag_citation_id_containing_14_does_not_supply_the_answer_value():
    answer = rag_answer()
    answer["citations"][0]["chunk_id"] = "archive:14-days"
    answer["answer"] = "The duration is unspecified [archive:14-days]."
    assert "14" in answer["answer"]
    with pytest.raises(ValueError, match="^qwen_answer_invalid$"):
        validate_rag_generation(answer)


@pytest.mark.parametrize("fault,code", [
    ("foreign_document", "identity_invalid"), ("changed_source", "identity_invalid"),
    ("missing_reference", "reference_invalid"), ("unknown_reference", "reference_invalid"),
    ("uncited_source", "reference_invalid"),
])
def test_rag_references_match_precise_returned_archive_evidence(fault, code):
    answer = rag_answer()
    if fault == "foreign_document":
        answer["citations"][0]["document_id"] = "different-document"
    elif fault == "changed_source":
        answer["citations"][0]["text"] = "Different source content mentions 14 days."
    elif fault == "missing_reference":
        answer["answer"] = "Sample archive exports are retained for 14 days."
    elif fault == "unknown_reference":
        answer["answer"] = "Sample archive exports are retained for 14 days [unknown]."
    else:
        answer["citations"].append({**source(), "chunk_id": "archive:another"})
    with pytest.raises(ValueError, match="^qwen_citation_" + code + "$"):
        validate_rag_generation(answer)


@pytest.mark.parametrize("fault,code", [
    ("missing_usage", "usage_unreported"), ("unreported", "usage_unreported"),
    ("empty_reports", "generation_usage_missing"), ("embedding_only", "generation_usage_missing"),
    ("wrong_model", "generation_usage_missing"), ("zero_prompt", "usage_tokens_invalid"),
    ("boolean_prompt", "usage_tokens_invalid"), ("zero_completion", "usage_tokens_invalid"),
    ("missing_completion", "usage_tokens_invalid"), ("zero_calls", "usage_count_invalid"),
])
def test_rag_requires_real_qwen_generation_usage_with_positive_integer_tokens(fault, code):
    answer = rag_answer()
    usage_report = answer["usage"]
    record = usage_report["provider_reports"][0]
    if fault == "missing_usage":
        answer.pop("usage")
    elif fault == "unreported":
        usage_report["token_usage"] = "unavailable"
    elif fault == "empty_reports":
        usage_report["provider_reports"] = []
    elif fault == "embedding_only":
        record["kind"] = "embedding"
    elif fault == "wrong_model":
        record["model"] = "router:another-model"
    elif fault == "zero_prompt":
        record["usage"]["prompt_tokens"] = 0
    elif fault == "boolean_prompt":
        record["usage"]["prompt_tokens"] = True
    elif fault == "zero_completion":
        record["usage"]["completion_tokens"] = 0
    elif fault == "missing_completion":
        record["usage"].pop("completion_tokens")
    else:
        usage_report["calls"] = 0
    with pytest.raises(ValueError, match="^qwen_rag_" + code + "$"):
        validate_rag_generation(answer)


def test_rag_reports_generation_tokens_separately_from_embedding_usage():
    answer = rag_answer()
    answer["usage"]["calls"] = 2
    answer["usage"]["provider_reports"].append({
        "kind": "embedding", "model": "local", "usage": {"prompt_tokens": 99},
    })
    result = validate_rag_generation(answer)
    assert result["reported_generation_calls"] == 1
    assert result["usage"] == {"prompt_tokens": 30, "completion_tokens": 20}


def test_rag_refusal_and_malformed_payloads_do_not_echo_input_in_errors():
    answer = rag_answer()
    answer["outcome"] = "insufficient_evidence"
    with pytest.raises(ValueError, match="^qwen_rag_generation_failed$"):
        validate_rag_generation(answer)
    with pytest.raises(ValueError, match="^qwen_acceptance_state_invalid$"):
        validate_rag_generation({"private": "confidential-content"})
