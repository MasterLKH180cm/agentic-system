"""Finite shared RAG decisions; ranked candidates never imply answer support."""
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = 2
POLICY_VERSION = "evidence-admission-v1"
REFUSAL_REASONS = frozenset({
    "no_candidates", "below_admission_threshold", "model_rejected", "context_budget_exhausted",
})
ADMISSION_BASES = frozenset({
    "no_candidates", "ranked_unassessed", "calibrated_reranker", "calibrated_answerability",
    "model_review", "structured_exact", "context_budget",
})
RefusalReason = Literal[
    "no_candidates", "below_admission_threshold", "model_rejected", "context_budget_exhausted",
]


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_version: Literal["evidence-admission-v1"]
    decision: Literal["accept", "abstain", "unassessed"]
    basis: Literal[
        "no_candidates", "ranked_unassessed", "calibrated_reranker", "calibrated_answerability",
        "model_review", "structured_exact", "context_budget",
    ]
    profile_id: str | None = Field(max_length=100)
    score: float | None = Field(allow_inf_nan=False)
    threshold: float | None = Field(allow_inf_nan=False)
    candidate_count: int = Field(ge=0, le=20)
    accepted_count: int = Field(ge=0, le=20)

    @model_validator(mode="after")
    def consistent(self):
        if self.accepted_count > self.candidate_count:
            raise ValueError("Admission counts conflict")
        if self.profile_id is not None and (
            not self.profile_id or self.profile_id != self.profile_id.strip()
            or any(ord(char) < 32 for char in self.profile_id)
        ):
            raise ValueError("Invalid admission profile identifier")
        if self.basis in {"calibrated_reranker", "calibrated_answerability"}:
            if self.profile_id is None or self.score is None or self.threshold is None:
                raise ValueError("Calibrated admission requires profile and finite scores")
            if self.decision == "accept" and self.score < self.threshold:
                raise ValueError("Calibrated acceptance is below its threshold")
            if self.decision == "abstain" and self.score >= self.threshold:
                raise ValueError("Calibrated refusal contradicts its threshold")
        elif any(value is not None for value in (self.profile_id, self.score, self.threshold)):
            raise ValueError("Uncalibrated admission cannot carry profile scores")
        return self


class RetrievalAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["success", "insufficient_evidence"]
    supported: bool | None
    refusal_reason: RefusalReason | None
    admission: Admission

    @model_validator(mode="after")
    def consistent(self):
        gate = self.admission
        if self.outcome == "insufficient_evidence":
            if self.supported is not False or self.refusal_reason is None or gate.decision != "abstain" or gate.accepted_count:
                raise ValueError("Contradictory retrieval refusal")
            expected = {"no_candidates": {"no_candidates"},
                        "below_admission_threshold": {"calibrated_reranker", "calibrated_answerability"},
                        "model_rejected": {"model_review"}, "context_budget_exhausted": {"context_budget"}}
            if gate.basis not in expected[self.refusal_reason]:
                raise ValueError("Refusal reason conflicts with admission basis")
            if self.refusal_reason == "no_candidates" and gate.candidate_count:
                raise ValueError("No-candidate refusal contains candidates")
        elif self.supported is False or self.refusal_reason is not None or gate.decision == "abstain":
            raise ValueError("Contradictory retrieval success")
        elif gate.decision == "unassessed":
            if (self.supported is not None or gate.basis != "ranked_unassessed"
                    or any(value is not None for value in (gate.profile_id, gate.score, gate.threshold))):
                raise ValueError("Unassessed evidence cannot claim semantic admission")
        elif gate.basis in {"model_review", "structured_exact", "calibrated_answerability"}:
            if self.supported is not True:
                raise ValueError("Explicit assessment requires its supported result")
        elif gate.basis not in {"calibrated_reranker", "calibrated_answerability"}:
            raise ValueError("Invalid successful admission basis")
        return self


def legacy_assessment(count: int, *, refused: bool = False) -> dict:
    reason = "model_rejected" if refused and count else "no_candidates"
    insufficient = refused or count == 0
    return {
        "outcome": "insufficient_evidence" if insufficient else "success",
        "supported": False if insufficient else None,
        "refusal_reason": reason if insufficient else None,
        "admission": {
            "policy_version": POLICY_VERSION, "decision": "abstain" if insufficient else "unassessed",
            "basis": ("model_review" if reason == "model_rejected" else "no_candidates") if insufficient else "ranked_unassessed",
            "profile_id": None, "score": None, "threshold": None,
            "candidate_count": count, "accepted_count": 0 if insufficient else count,
        },
    }


def parse_assessment(data: dict, citation_count: int) -> dict:
    """Normalize genuine legacy replies, but never downgrade malformed typed ones."""
    fields = {"outcome", "supported", "refusal_reason", "admission"}
    if not ({"outcome", "refusal_reason", "admission"} & data.keys()):
        if data.get("supported") is not None and type(data["supported"]) is not bool:
            raise ValueError("Invalid legacy support flag")
        return legacy_assessment(citation_count, refused=data.get("supported") is False)
    if not fields <= data.keys():
        raise ValueError("Incomplete retrieval assessment")
    result = RetrievalAssessment.model_validate({key: data[key] for key in fields}).model_dump()
    if result["outcome"] == "insufficient_evidence" and citation_count:
        raise ValueError("Refused retrieval returned usable citations")
    if result["admission"]["accepted_count"] != citation_count:
        raise ValueError("Admission count does not match returned evidence")
    return result


def normalize_structured(value) -> dict | None:
    """Only finite exact aggregates with complete dataset provenance are evidence."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid structured result")
    aggregate, provenance = value.get("aggregate"), value.get("provenance")
    if aggregate is None:
        return None
    if not isinstance(aggregate, dict) or set(aggregate) - {"operation", "field", "value", "matched_rows"}:
        raise ValueError("Invalid structured aggregate")
    operation, number, count = aggregate.get("operation"), aggregate.get("value"), aggregate.get("matched_rows")
    if operation not in {"count", "sum", "mean", "min", "max"}:
        raise ValueError("Invalid structured operation")
    if type(number) not in (int, float) or not math.isfinite(number):
        raise ValueError("Structured aggregate must be finite")
    if type(count) is not int or not 0 <= count <= 1_000_000:
        raise ValueError("Invalid structured row count")
    if "matched_rows" in value and (type(value["matched_rows"]) is not int or value["matched_rows"] != count):
        raise ValueError("Structured row counts conflict")
    if operation == "count" and (type(number) is not int or number != count):
        raise ValueError("Structured count does not match its result")
    if operation == "sum" and count == 0 and number != 0:
        raise ValueError("Empty structured sum must be zero")
    if operation in {"mean", "min", "max"} and count == 0:
        raise ValueError("Empty numeric population has no finite aggregate")
    field = aggregate.get("field")
    if operation != "count" and (not isinstance(field, str) or not 1 <= len(field) <= 128):
        raise ValueError("Structured numeric aggregate requires a field")
    if field is not None and (not isinstance(field, str) or not 1 <= len(field) <= 128):
        raise ValueError("Invalid structured field")
    if not isinstance(provenance, dict) or set(provenance) != {"datasets"}:
        raise ValueError("Structured evidence requires dataset provenance")
    datasets = provenance["datasets"]
    if not isinstance(datasets, list) or not 1 <= len(datasets) <= 20:
        raise ValueError("Structured provenance requires 1..20 datasets")
    clean, seen = [], set()
    for dataset in datasets:
        if not isinstance(dataset, dict) or set(dataset) != {"document_id", "revision", "profile_id"}:
            raise ValueError("Invalid structured dataset identity")
        for key, limit in (("document_id", 250), ("revision", 100), ("profile_id", 100)):
            if not isinstance(dataset[key], str) or not 1 <= len(dataset[key]) <= limit:
                raise ValueError("Invalid structured dataset identity")
        identity = (dataset["document_id"], dataset["revision"])
        if identity in seen:
            raise ValueError("Duplicate structured dataset")
        seen.add(identity)
        clean.append(dict(dataset))
    return {"aggregate": {"operation": operation, "field": field, "value": number, "matched_rows": count},
            "provenance": {"datasets": clean}}


def require_admission(assessment: dict, structured: dict | None, policy: str, profiles: list | tuple) -> None:
    if policy not in {"compatibility", "calibrated_required"}:
        raise PermissionError("Unknown snapshotted RAG admission policy")
    if assessment["outcome"] == "insufficient_evidence":
        return
    gate = assessment["admission"]
    if gate["basis"] == "structured_exact" and structured is None:
        raise ValueError("Exact structured admission has no valid aggregate")
    if not gate["accepted_count"] and structured is None:
        raise ValueError("Successful query has no usable evidence")
    if policy == "calibrated_required" and not (
        gate["decision"] == "accept" and (
            (gate["basis"] == "structured_exact" and structured is not None)
            or (gate["basis"] in {"calibrated_reranker", "calibrated_answerability"} and gate["profile_id"] in profiles)
        )
    ):
        raise PermissionError("RAG evidence does not satisfy the snapshotted admission policy")


def safe_metadata(result: dict, profiles: list | tuple = ()) -> dict:
    """Event fields contain enums/counts, never provider explanations or sources."""
    output = {}
    for key in ("refusal_reason", "last_refusal_reason"):
        if result.get(key) in REFUSAL_REASONS:
            output[key] = result[key]
    retrieval = result.get("retrieval", {})
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    gate = retrieval.get("admission", {})
    if isinstance(gate, dict):
        if gate.get("decision") in {"accept", "abstain", "unassessed"}:
            output["admission_decision"] = gate["decision"]
        if gate.get("basis") in ADMISSION_BASES:
            output["admission_basis"] = gate["basis"]
        if gate.get("profile_id") in profiles:
            output["admission_profile_id"] = gate["profile_id"]
        for key in ("candidate_count", "accepted_count"):
            value = gate.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                output[key] = value
    if retrieval.get("outcome") in {"success", "insufficient_evidence"}:
        output["retrieval_outcome"] = retrieval["outcome"]
    if retrieval.get("refusal_reason") in REFUSAL_REASONS:
        output["refusal_reason"] = retrieval["refusal_reason"]
    if isinstance(result.get("citations"), list):
        output["usable_sources"] = min(20, len(result["citations"]))
    if isinstance(result.get("observations"), list):
        output["usable_observations"] = min(24, len(result["observations"]))
    upstream = result.get("upstream", {})
    if isinstance(upstream, dict) and upstream.get("engine") in {"raptor", "lightrag"}:
        output["upstream_engine"] = upstream["engine"]
        if type(upstream.get("engine_executed")) is bool:
            output["upstream_executed"] = upstream["engine_executed"]
        if type(upstream.get("source_count")) is int and 0 <= upstream["source_count"] <= 64:
            output["upstream_source_count"] = upstream["source_count"]
    return output


def upstream_identity(trace) -> dict | None:
    """Keep reported implementation identity, never copy arbitrary backend trace."""
    if trace is None:
        return None
    if not isinstance(trace, list) or len(trace) > 128:
        raise ValueError("Invalid RAG trace envelope")
    records = []
    for event in trace:
        if not isinstance(event, dict) or event.get("operation") != "external_backend" or "implementation" not in event:
            continue
        engine = event.get("backend")
        implementation, diagnostics = event["implementation"], event.get("diagnostics")
        if (engine not in {"raptor", "lightrag"} or not isinstance(implementation, dict)
                or set(implementation) != {"library", "version", "source_commit", "adapter_version"}
                or not isinstance(diagnostics, dict)):
            raise ValueError("Invalid reported upstream identity")
        if implementation["library"] != {"raptor": "raptor", "lightrag": "lightrag-hku"}[engine]:
            raise ValueError("Upstream library does not match its engine")
        if (not isinstance(implementation["source_commit"], str)
                or not re.fullmatch(r"[a-f0-9]{40}", implementation["source_commit"])
                or implementation["adapter_version"] != "v03"
                or not isinstance(implementation["version"], str)
                or not re.fullmatch(r"[a-zA-Z0-9_.+-]{1,80}", implementation["version"])):
            raise ValueError("Invalid upstream version identity")
        count, executed = diagnostics.get("source_count"), diagnostics.get("engine_executed")
        if type(count) is not int or not 0 <= count <= 64 or type(executed) is not bool or executed is not bool(count):
            raise ValueError("Invalid reported upstream execution counts")
        records.append({"engine": engine, "implementation": dict(implementation),
                        "source_count": count, "engine_executed": executed})
    if len(records) > 1:
        raise ValueError("Ambiguous upstream execution identity")
    return records[0] if records else None
