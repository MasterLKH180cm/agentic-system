"""Read-only attribution of a complete frozen Qwen admission verification.

Standard library only: never imports RAG/model code or executes inference.
Original reports, profiles and datasets are immutable inputs. Optional output is
created exclusively, and contains synthetic IDs/counters rather than source text.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


COVERAGES = ("not_applicable", "none", "partial", "complete")
REMOVAL_CAUSES = ("admission_refusal", "context_budget", "unattributed")
GENERATIONS = ("generate_false", "generate_true")
MODES = ("hybrid", "structured", "temporal")
CORRECTNESS_GATES = {
    f"{generation}_{mode}_{check}" for generation in GENERATIONS for mode in MODES
    for check in ("execution", "citation_records", "typed_outcomes")
} | {f"{mode}_generation_mode_consistency" for mode in MODES}
QUALITY_GATES = {"holdout_positive_retention", "holdout_negative_refusal", "holdout_semantic_only"} | {
    f"{generation}_{check}" for generation in GENERATIONS
    for check in ("hybrid_admitted_recall_at_3", "hybrid_no_answer_refusals", "structured_exact", "temporal_exact")
}


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON constant")

    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    return value, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def reference_detail(case, measurement, k):
    """Same document-ID definitions as rags.evaluate, without importing a runtime."""
    relevant = set(case["relevant_document_ids"])
    raw = list(dict.fromkeys(measurement["raw_candidate_document_ids"]))[:k]
    admitted = list(dict.fromkeys(measurement["retrieved_document_ids"]))[:k]
    raw_reference = [key for key in raw if key in relevant]
    admitted_reference = [key for key in admitted if key in relevant]
    removed = [key for key in raw_reference if key not in set(admitted_reference)]
    coverage = ("not_applicable" if not relevant else "none" if not raw_reference
                else "complete" if len(raw_reference) == len(relevant) else "partial")
    cause = None
    if removed:
        cause = "unattributed"
        if measurement["outcome"] == "insufficient_evidence":
            if measurement["refusal_reason"] in {"below_admission_threshold", "model_rejected"}:
                cause = "admission_refusal"
            elif measurement["refusal_reason"] == "context_budget_exhausted":
                cause = "context_budget"
    return {"reference_document_ids": sorted(relevant), "raw_reference_document_ids": raw_reference,
            "admitted_reference_document_ids": admitted_reference,
            "missing_raw_reference_document_ids": sorted(relevant - set(raw_reference)),
            "removed_reference_document_ids": removed, "raw_reference_coverage": coverage,
            "reference_removal_cause": cause}


def reference_summary(rows):
    coverage, refusals = dict.fromkeys(COVERAGES, 0), dict.fromkeys(COVERAGES, 0)
    removals = {cause: {"cases": 0, "document_occurrences": 0} for cause in REMOVAL_CAUSES}
    for row in rows:
        diagnostic = row["reference_diagnostics"]
        category = diagnostic["raw_reference_coverage"]
        coverage[category] += 1
        if not row["unanswerable"] and row["outcome"] == "insufficient_evidence":
            refusals[category] += 1
        if diagnostic["removed_reference_document_ids"]:
            count = removals[diagnostic["reference_removal_cause"]]
            count["cases"] += 1
            count["document_occurrences"] += len(diagnostic["removed_reference_document_ids"])
    return {"scope": "document_id_coverage_only", "cases": len(rows),
            "raw_reference_coverage": coverage, "answerable_refusals_by_raw_reference_coverage": refusals,
            "reference_removals": {
                "cases": sum(item["cases"] for item in removals.values()),
                "document_occurrences": sum(item["document_occurrences"] for item in removals.values()),
                "by_cause": removals,
            }}


def gate_summary(recorded, expected_names):
    gates = recorded["gates"]
    require(len(gates) == len(expected_names) and {gate["name"] for gate in gates} == expected_names,
            "Incomplete or duplicate gate group")
    require(all(type(gate["passed"]) is bool for gate in gates), "Invalid gate boolean")
    failures = [gate for gate in gates if gate["passed"] is not True]
    require(type(recorded["passed"]) is bool and recorded["passed"] is (not failures),
            "Gate summary flag differs from individual gates")
    require(type(recorded["passed_count"]) is int and type(recorded["failed_count"]) is int,
            "Invalid gate counter types")
    require(recorded["passed_count"] == len(gates) - len(failures), "Inconsistent recorded gate counts")
    require(recorded["failed_count"] == len(failures), "Inconsistent recorded gate failures")
    require(recorded["failures"] == [gate["name"] for gate in failures], "Inconsistent recorded gate names")
    return {"passed": recorded["passed"], "passed_count": len(gates) - len(failures),
            "total_count": len(gates), "failed_gates": failures}


def analyze(report_path, root):
    paths = {"report": report_path, "profile": root / "profiles/admission-qwen-v03.json",
             "holdout_dataset": root / "examples/admission_holdout_v03.json",
             "regression_dataset": root / "examples/eval_v02.json"}
    objects, inputs = {}, {}
    for name, path in paths.items():
        objects[name], inputs[name] = read(path)
    report, profile, holdout, regression = (objects[name] for name in paths)
    require(report["schema_version"] == "admission-verification-v1", "A complete verification report is required")
    require(profile["scorer"]["model"] == "Qwen/Qwen3-1.7B", "Expected frozen Qwen scorer")
    require(report["profile_id"] == profile["profile_id"] == report["holdout"]["profile_id"], "Profile identity mismatch")
    require(report["profile_file_sha256"] == inputs["profile"]["sha256"], "Profile file digest mismatch")
    require(report["frozen_threshold"] == profile["threshold"] == report["holdout"]["threshold"], "Threshold mismatch")
    require(report["holdout"]["scorer"] == profile["scorer"], "Scorer identity mismatch")
    require(profile["scorer"]["limits"]["batch_size"] == 1
            and profile["scorer"]["aggregation"] == "max_original_passage", "Unsupported forward-count contract")
    require(digest(holdout["cases"]) == holdout["cases_digest"]["value"] == report["holdout"]["cases_digest"],
            "Frozen holdout digest mismatch")
    holdout_cases = {case["id"]: case for case in holdout["cases"]}
    held_rows = report["holdout"]["measurements"]
    require({row["id"] for row in held_rows} == set(holdout_cases) and len(held_rows) == len(holdout_cases),
            "Holdout measurement set mismatch")
    holdout_forwards = 0
    for row in held_rows:
        expected = len(holdout_cases[row["id"]]["passages"])
        require(len(row["passage_scores"]) == len(row["original_pair_tokens"]) == expected,
                "Holdout passage measurements mismatch")
        holdout_forwards += expected

    regression_cases = {case["id"]: case for case in regression["cases"]}
    require(set(report["regression"]) == set(GENERATIONS), "Incomplete or unexpected generation groups")
    modes, forward_rows, ambiguous = {}, [], []
    for generation, reports in report["regression"].items():
        require(set(reports) == set(MODES), "Incomplete or unexpected mode groups")
        modes[generation] = {}
        for mode, recorded in reports.items():
            expected_cases = {case["id"] for case in regression["cases"]
                              if not case.get("modes") or mode in case["modes"]}
            measurements = recorded["measurements"]
            require(recorded["k"] == 3, "Unexpected regression retrieval k")
            require(recorded["status"] == "available" and not recorded["errors"], "Incomplete regression execution")
            require({row["id"] for row in measurements} == expected_cases and len(measurements) == len(expected_cases),
                    "Regression measurement set mismatch")
            require(recorded["cases"] == recorded["evaluated_cases"] == len(expected_cases),
                    "Regression case counts mismatch")
            rows, present, forwards = [], 0, 0
            basis_counts, scored_sizes = Counter(), Counter()
            for measurement in measurements:
                case = regression_cases[measurement["id"]]
                diagnostic = reference_detail(case, measurement, recorded["k"])
                if "reference_diagnostics" in measurement:
                    present += 1
                    require(measurement["reference_diagnostics"] == diagnostic, "Recorded per-case diagnostic mismatch")
                row = {"id": measurement["id"], "category": case.get("category"),
                       "unanswerable": bool(case.get("unanswerable")), "outcome": measurement["outcome"],
                       "refusal_reason": measurement["refusal_reason"], "reference_diagnostics": diagnostic}
                rows.append(row)
                admission = measurement["admission"]
                basis_counts[admission["basis"]] += 1
                if admission["basis"] == "calibrated_answerability":
                    forwards += admission["candidate_count"]
                    scored_sizes[str(admission["candidate_count"])] += 1
                elif admission["basis"] not in {"structured_exact", "no_candidates", "model_review"}:
                    # In particular, context_budget can replace prior scoring metadata.
                    ambiguous.append({"generation": generation, "mode": mode, "id": row["id"],
                                      "final_basis": admission["basis"]})
            summary = reference_summary(rows)
            require(sum(summary["answerable_refusals_by_raw_reference_coverage"].values()) == recorded["false_refusals"],
                    "Refusal decomposition differs from the unchanged metric")
            if "reference_diagnostics" in recorded:
                require(recorded["reference_diagnostics"] == summary, "Recorded aggregate diagnostic mismatch")
            forward_rows.append({"generation": generation, "mode": mode, "queries": len(measurements),
                                 "scored_queries": basis_counts["calibrated_answerability"],
                                 "forward_count": forwards, "basis_counts": dict(basis_counts),
                                 "scored_candidate_count_histogram": dict(scored_sizes)})
            modes[generation][mode] = {
                "diagnostic_origin": "recorded_and_recomputed_identical" if present == len(rows)
                                     else "derived_from_immutable_measurements_and_frozen_labels",
                "recorded_case_diagnostics": present, "reference_diagnostics": summary,
                "raw_recall_at_k": recorded["raw_candidates"]["recall_at_k"],
                "admitted_recall_at_k": recorded["admitted_evidence"]["recall_at_k"],
                "false_refusals": recorded["false_refusals"], "true_refusals": recorded["true_refusals"],
                "raw_reference_miss_ids": [row["id"] for row in rows
                                           if row["reference_diagnostics"]["raw_reference_coverage"] == "none"],
                "partial_reference_ids": [row["id"] for row in rows
                                          if row["reference_diagnostics"]["raw_reference_coverage"] == "partial"],
                "diagnostic_cases": [row for row in rows if row["reference_diagnostics"]["raw_reference_coverage"]
                                     in {"none", "partial"} or row["reference_diagnostics"]["removed_reference_document_ids"]],
            }

    failed_predictions = [{key: row[key] for key in ("id", "answerable", "category", "score",
                                                    "truncated_passages", "original_pair_tokens")}
                          for row in held_rows if row["answerable"] != (row["score"] >= profile["threshold"])]
    scorer = profile["scorer"]
    correctness = gate_summary(report["correctness"], CORRECTNESS_GATES)
    quality = gate_summary(report["quality"], QUALITY_GATES)
    require(type(report["enforce_quality"]) is bool, "Invalid quality-enforcement boolean")
    promotion = "passed" if correctness["passed"] and quality["passed"] else "failed"
    expected_exit = 1 if not correctness["passed"] else 2 if report["enforce_quality"] and not quality["passed"] else 0
    require(report["promotion_status"] == promotion, "Promotion status differs from recorded gate results")
    require(type(report["exit_status"]) is int and report["exit_status"] == expected_exit,
            "Exit status differs from gates and enforce_quality")
    source_hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((root / "rags").rglob("*.py"))}
    return {
        "schema_version": "qwen-admission-readonly-analysis-v2", "analysis_executed_model_inference": False,
        "analysis_provenance": {
            "analyzed_at": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
            "helper": {"file": Path(__file__).name, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            "rags_source_sha256": source_hashes,
            "source_hash_scope": "Snapshot at analysis time; not retroactive attestation of the native execution",
        },
        "report_measured_at": report["measured_at"], "inputs": inputs,
        "profile": {"profile_id": profile["profile_id"], "threshold": profile["threshold"],
                    **{key: scorer[key] for key in ("model", "revision", "runtime", "aggregation", "score_convention")}},
        "correctness": correctness, "quality": quality,
        "enforce_quality": report["enforce_quality"],
        "promotion_status": report["promotion_status"], "exit_status": report["exit_status"],
        "recorded_elapsed_seconds": report["elapsed_seconds"],
        "forward_count": {"kind": "reconstructed_not_instrumented",
                          "method": "One batch-size-1 forward per scored passage; candidate_count counts chunks, not unique document IDs",
                          "holdout": holdout_forwards, "regression": forward_rows,
                          "derived_total": holdout_forwards + sum(row["forward_count"] for row in forward_rows),
                          "exact_from_observed_scoring_metadata": not ambiguous, "ambiguous_cases": ambiguous},
        "holdout_metrics": report["holdout"]["metrics"], "holdout_incorrect_predictions": failed_predictions,
        "regression": modes,
        "limits": ["Document ID coverage does not establish semantic sufficiency or entailment.",
                   "The forward count is reconstructed from complete report metadata and the reviewed scoring implementation, not an instrumented runtime counter.",
                   "generate_true uses the extractive provider; these are not chat decoding speeds or TTFT.",
                   "The historical report binds the holdout digest but does not embed a regression fixture digest; the supplied frozen fixture is separately hashed here.",
                   "Analysis changes no gates, thresholds, profiles, datasets, predictions or original measurements."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rags-root", type=Path, required=True, help="Explicit checkout containing the frozen v03 profile and datasets")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Optional new derivative file; never overwrites an existing path")
    args = parser.parse_args()
    result = analyze(args.report, args.rags_root)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps({"analysis": str(args.output.resolve()), "source_measured_at": result["report_measured_at"],
                          "forwards": result["forward_count"]["derived_total"],
                          "correctness_passed": result["correctness"]["passed_count"],
                          "quality_passed": result["quality"]["passed_count"]}))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
