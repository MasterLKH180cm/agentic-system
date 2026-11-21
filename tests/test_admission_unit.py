"""Pure refusal, exact-result provenance and inherited-evidence boundaries."""
import copy
import json

import pytest

from agentic.admission import (
    Admission,
    legacy_assessment,
    normalize_structured,
    parse_assessment,
    require_admission,
    safe_metadata,
    upstream_identity,
)
from agentic.evidence import collect_evidence, normalize_citations, strict_json_loads

pytestmark = pytest.mark.unit


def calibrated_gate(**changes):
    return {
        "policy_version": "evidence-admission-v1", "decision": "accept",
        "basis": "calibrated_reranker", "profile_id": "fixture-profile", "score": 0.5,
        "threshold": 0.5, "candidate_count": 1, "accepted_count": 1, **changes,
    }


def exact_result():
    return {
        "aggregate": {"operation": "count", "value": 0, "matched_rows": 0},
        "matched_rows": 0,
        "provenance": {"datasets": [{"document_id": "orders", "revision": "r1", "profile_id": "index-v1"}]},
    }


def source(identifier="c1", text="The session timeout is 30 minutes."):
    return {"chunk_id": identifier, "document_id": "policy", "text": text}


def test_legacy_support_never_becomes_calibrated_and_partial_typed_replies_never_downgrade():
    legacy = parse_assessment({"supported": True}, 1)
    assert legacy["supported"] is None
    assert legacy["admission"]["decision"] == "unassessed"
    assert parse_assessment({"supported": False}, 1)["refusal_reason"] == "model_rejected"
    assert parse_assessment({}, 0)["refusal_reason"] == "no_candidates"
    with pytest.raises(ValueError, match="legacy support"):
        parse_assessment({"supported": "yes"}, 1)
    for partial in ({"outcome": "success"}, {"admission": {}}, {"refusal_reason": None}):
        with pytest.raises(ValueError, match="Incomplete"):
            parse_assessment(partial, 1)


@pytest.mark.parametrize("changes", [
    {"candidate_count": 0}, {"profile_id": " profile "}, {"profile_id": "bad\nprofile"},
    {"score": None}, {"score": 0.49}, {"decision": "abstain", "accepted_count": 0},
    {"score": float("inf")},
])
def test_calibrated_decision_requires_consistent_counts_identity_and_threshold(changes):
    with pytest.raises(ValueError):
        Admission.model_validate(calibrated_gate(**changes))


def test_assessment_refusal_reason_basis_and_evidence_count_are_consistent():
    refusal = legacy_assessment(0)
    invalid = copy.deepcopy(refusal)
    invalid["refusal_reason"] = "model_rejected"
    with pytest.raises(ValueError, match="basis"):
        parse_assessment(invalid, 0)
    invalid = copy.deepcopy(refusal)
    invalid["admission"]["candidate_count"] = 1
    with pytest.raises(ValueError, match="No-candidate"):
        parse_assessment(invalid, 0)
    with pytest.raises(ValueError, match="usable citations"):
        parse_assessment(refusal, 1)
    with pytest.raises(ValueError, match="does not match"):
        parse_assessment(legacy_assessment(2), 1)
    unassessed = legacy_assessment(1)
    unassessed["supported"] = True
    with pytest.raises(ValueError, match="Unassessed"):
        parse_assessment(unassessed, 1)
    no_evidence = legacy_assessment(1)
    no_evidence["admission"]["accepted_count"] = 0
    with pytest.raises(ValueError, match="no usable evidence"):
        require_admission(no_evidence, None, "compatibility", [])
    with pytest.raises(PermissionError, match="Unknown"):
        require_admission(refusal, None, "unregistered-policy", [])


@pytest.mark.parametrize("aggregate,reason", [
    ({"operation": "count", "value": True, "matched_rows": 0}, "finite"),
    ({"operation": "sum", "field": "amount", "value": float("inf"), "matched_rows": 1}, "finite"),
    ({"operation": "count", "value": 0, "matched_rows": False}, "row count"),
    ({"operation": "count", "value": 1, "matched_rows": 0}, "count does not match"),
    ({"operation": "sum", "field": "amount", "value": 1, "matched_rows": 0}, "must be zero"),
    ({"operation": "mean", "field": "amount", "value": 0, "matched_rows": 0}, "no finite aggregate"),
    ({"operation": "sum", "value": 0, "matched_rows": 0}, "requires a field"),
    ({"operation": "count", "field": "", "value": 0, "matched_rows": 0}, "Invalid structured field"),
    ({"operation": "median", "value": 0, "matched_rows": 0}, "operation"),
    ({"operation": "count", "value": 0, "matched_rows": 0, "sql": "SELECT 1"}, "aggregate"),
])
def test_exact_aggregates_cannot_claim_inconsistent_or_nonfinite_values(aggregate, reason):
    value = exact_result()
    value["aggregate"] = aggregate
    with pytest.raises(ValueError, match=reason):
        normalize_structured(value)


def test_exact_provenance_is_required_unique_and_bounded():
    valid = exact_result()
    for invalid in ([], {**valid, "aggregate": []}, {**valid, "matched_rows": 1},
                    {**valid, "provenance": {"datasets": [], "query": "private"}},
                    {**valid, "provenance": {"datasets": [valid["provenance"]["datasets"][0]] * 2}},
                    {**valid, "provenance": {"datasets": [{"document_id": "orders"}]}},
                    {**valid, "provenance": {"datasets": [{"document_id": "", "revision": "r", "profile_id": "p"}]}}):
        with pytest.raises(ValueError):
            normalize_structured(invalid)
    assert normalize_structured(None) is None
    assert normalize_structured({"rows": []}) is None
    normalized = normalize_structured(valid)
    assert normalized["aggregate"]["value"] == 0
    assert normalized["provenance"] == valid["provenance"]
    assessment = legacy_assessment(1)
    assessment.update(supported=True)
    assessment["admission"].update(decision="accept", basis="structured_exact", candidate_count=0, accepted_count=0)
    parsed = parse_assessment(assessment, 0)
    require_admission(parsed, normalized, "calibrated_required", [])
    with pytest.raises(ValueError, match="no valid aggregate"):
        require_admission(parsed, None, "compatibility", [])


@pytest.mark.parametrize("change", [
    {"chunk_id": "unsafe[id]"}, {"document_id": ""}, {"text": "  "}, {"title": 7},
    {"start_char": True}, {"end_char": -1}, {"score": float("nan")}, {"score": False},
    {"source_run_id": "r" * 81},
])
def test_source_identity_metadata_and_offsets_are_strict(change):
    with pytest.raises(ValueError):
        normalize_citations([{**source(), **change}])


def test_source_normalization_preserves_bounded_provenance_and_rejects_collision():
    normalized, = normalize_citations([{**source(text="x" * 4001), "title": "t" * 201,
                                       "start_char": 0, "end_char": 4001, "source_run_id": "run1", "score": 0.0}])
    assert len(normalized["text"]) == 4000 and normalized["text_truncated"]
    assert len(normalized["title"]) == 200
    assert normalized["start_char"] == 0 and normalized["source_run_id"] == "run1"
    for citations in ({}, [None], [source()] * 21, [source(), source(text="Conflicting content")]):
        with pytest.raises(ValueError):
            normalize_citations(citations)


def test_refused_parent_quarantines_nested_evidence_across_other_wrappers():
    bundle = collect_evidence({
        "refused": {"outcome": "insufficient_evidence", "refusal_reason": "model_rejected",
                    "outputs": {"child": {"citations": [source()]}}},
        "repackaged": {"outputs": {"child": {"citations": [source(), source("c2")]}}},
    })
    assert set(bundle.sources) == {"c2"}
    assert bundle.quarantined_ids == {"c1"}
    assert bundle.refusal_reason == "model_rejected"


def test_inherited_source_identity_may_extend_only_a_marked_truncated_prefix():
    short = {**source(text="The session"), "text_truncated": True}
    full = source()
    bundle = collect_evidence({"short": {"citations": [short]}, "full": {"citations": [full]}})
    assert bundle.sources["c1"]["text"] == full["text"]
    with pytest.raises(ValueError, match="Conflicting source identity"):
        collect_evidence({"short": {"citations": [{**short, "text_truncated": False}]},
                          "full": {"citations": [full]}})
    with pytest.raises(ValueError, match="inherited source admission"):
        collect_evidence({"summary": {"citations": [full], "source_admissions": {"foreign": {}}}})


def test_inherited_evidence_is_bounded_and_cannot_forge_arithmetic_values():
    nested = {}
    for _ in range(7):
        nested = {"outputs": {"child": nested}}
    invalid_inputs = [
        {"root": nested},
        {"root": {"outputs": {str(index): {} for index in range(25)}}},
        {"root": {"quarantined_ids": ["unsafe[id]"]}},
        {"root": {"observations": {}}},
        {"root": {"observations": [None]}},
        {"root": {"observations": [{"kind": "arithmetic", "value": True}]}},
        {f"step{index}": {"value": index} for index in range(49)},
    ]
    for inputs in invalid_inputs:
        with pytest.raises(ValueError):
            collect_evidence(inputs)
    bundle = collect_evidence({"summary": {"observations": [
        {"kind": "arithmetic", "value": 0, "expression": "2 - 2"}
    ]}})
    assert bundle.observations == [{"step": "summary", "kind": "arithmetic", "value": 0, "expression": "2 - 2"}]


def test_durable_metadata_keeps_only_allowlisted_bounded_fields():
    metadata = safe_metadata({
        "refusal_reason": "private-provider-explanation",
        "retrieval": {"outcome": "success", "admission": {
            "decision": "accept", "basis": "calibrated_reranker", "profile_id": "private-profile",
            "candidate_count": 5, "accepted_count": True,
        }},
        "citations": [source()] * 30, "observations": [{}] * 30,
        "upstream": {"engine": "raptor", "engine_executed": True, "source_count": 65,
                     "raw_trace": "private-source-text"},
    }, profiles=["fixture-profile"])
    assert metadata == {
        "admission_decision": "accept", "admission_basis": "calibrated_reranker",
        "candidate_count": 5, "retrieval_outcome": "success", "usable_sources": 20,
        "usable_observations": 24, "upstream_engine": "raptor", "upstream_executed": True,
    }
    assert "private" not in json.dumps(metadata)


def test_upstream_identity_rejects_ambiguous_or_unbounded_trace_envelopes():
    event = {"operation": "external_backend", "backend": "raptor",
             "implementation": {"library": "raptor", "version": "1.0", "source_commit": "a" * 40,
                                "adapter_version": "v03"},
             "diagnostics": {"source_count": 1, "engine_executed": True}}
    identity = upstream_identity([{"operation": "unrelated", "private": "secret"}, event])
    assert identity["engine"] == "raptor" and "diagnostics" not in identity
    assert upstream_identity(None) is None
    assert upstream_identity(["ignored", {"operation": "other"}]) is None
    for trace in ({}, [{}] * 129, [event, copy.deepcopy(event)]):
        with pytest.raises(ValueError):
            upstream_identity(trace)


def test_model_json_rejects_duplicate_keys_and_nonfinite_numbers_at_any_depth():
    for raw in ('{"outcome":"success","outcome":"insufficient_evidence"}',
                '{"nested":{"value":1,"value":2}}', '{"value":NaN}', '{"value":Infinity}'):
        with pytest.raises(ValueError):
            strict_json_loads(raw)
    assert strict_json_loads('{"value":0,"supported":false}') == {"value": 0, "supported": False}
