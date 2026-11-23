"""Describe the completed Qwen direct/gateway JSON evidence without running models.

This scratch helper preserves failed quality gates, never computes a speedup, and
keeps all attempted latency separate from the benchmark's passed-only latency.
It imports only the Python standard library and refuses to overwrite evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys


WORKSPACE = Path("/workspace/scratch/7113f13a25c9")
CASES = {"json-extraction", "grounded-answer", "insufficient-evidence", "untrusted-instruction",
         "readonly-tool", "tool-argument-boundary"}
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "reasoning_tokens")
METHOD = "linear_interpolation_at_n_minus_one_times_q"


def canonical_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def read_report(path: Path) -> tuple[dict, dict]:
    with path.open("rb") as handle:
        data = handle.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Report exceeds bounded input size")
    value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Report must be a JSON object")
    return value, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                   "created_at": value.get("created_at"), "measured_at": value.get("measured_at")}


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def percentile(values: list[float], quantile: float):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latencies(samples: list[dict]) -> dict:
    values = [sample["latency_ms"] for sample in samples if number(sample.get("latency_ms"))]
    return {"sample_count": len(values), "complete": len(values) == len(samples),
            "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
            "percentile_method": METHOD}


def usage_summary(samples: list[dict]) -> dict:
    result = {}
    for field in USAGE_FIELDS:
        values = [sample.get("usage", {}).get(field) for sample in samples]
        reported = [value for value in values if type(value) is int and value >= 0]
        result[field] = {"sum_reported_values": sum(reported) if reported else None,
                         "report_count": len(reported), "all_attempts_reported": len(reported) == len(samples)}
    return result


def phase_analysis(phase: dict, name: str, rounds: int, issues: list[str]) -> dict:
    raw, samples = phase["summary"], phase["samples"]
    expected = {(case, repeat) for case in CASES for repeat in range(rounds)}
    identities = [(sample.get("case_id"), sample.get("repetition")) for sample in samples]
    if len(identities) != len(expected) or set(identities) != expected:
        issues.append(name + ": incomplete/duplicate/unexpected case repetitions")
    attempted = [sample for sample in samples if sample.get("status") != "not_run"]
    passed = [sample for sample in attempted if sample.get("status") == "passed"]
    failed = [sample for sample in attempted if sample.get("status") != "passed"]
    if any(sample.get("status") not in {"passed", "failed"} for sample in samples):
        issues.append(name + ": unattempted or unsupported sample status")
    if any(sample.get("schema_valid") is not True or sample.get("behavior_valid") is not True for sample in passed):
        issues.append(name + ": reported pass lacks schema/behavior validation")
    counts = {"planned": len(samples), "attempted": len(attempted), "passed": len(passed),
              "failed": len(failed), "not_run": len(samples) - len(attempted),
              "schema_valid": sum(sample.get("schema_valid") is True for sample in samples)}
    if any(raw.get(key) != value for key, value in counts.items()):
        issues.append(name + ": raw summary counts do not match samples")
    wall = raw.get("wall_seconds")
    if not number(wall) or wall <= 0:
        raise ValueError(name + ": invalid source wall time")
    all_latency, pass_latency = latencies(attempted), latencies(passed)
    if not all_latency["complete"]:
        issues.append(name + ": incomplete all-attempted latency")
    recomputed = {"p50_ms": pass_latency["p50_ms"], "p95_ms": pass_latency["p95_ms"],
                  "validated_requests_per_second": len(passed) / wall}
    if any(not number(raw.get(key)) or not math.isclose(raw[key], value, rel_tol=1e-6, abs_tol=1e-6)
           for key, value in recomputed.items() if value is not None):
        issues.append(name + ": raw passed-only statistics do not match samples")
    if raw.get("percentile_method") != METHOD or raw.get("successful_latency_sample_count") != len(passed):
        issues.append(name + ": unexpected raw percentile metadata")
    usage = usage_summary(attempted)
    for field, raw_total in raw.get("reported_token_totals", {}).items():
        if field in usage and raw_total != (usage[field]["sum_reported_values"] or 0):
            issues.append(name + ": raw token total mismatch for " + field)
    cache = usage["cached_prompt_tokens"]
    if not cache["all_attempts_reported"] or cache["sum_reported_values"] != 0:
        issues.append(name + ": cache tokens are nonzero or incompletely reported")
    for sample in attempted:
        values = sample.get("usage", {})
        if not all(type(values.get(key)) is int and values[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
            issues.append(name + ": incomplete provider usage")
            break
        if values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]:
            issues.append(name + ": inconsistent total token usage")
            break
    per_case = []
    for case in sorted(CASES):
        selected = [sample for sample in attempted if sample.get("case_id") == case]
        failures = [sample for sample in selected if sample.get("status") != "passed"]
        per_case.append({"case_id": case, "attempted": len(selected), "passed": len(selected) - len(failures),
                         "failed": len(failures), "failure_codes": dict(Counter(item.get("failure_code") or "unspecified" for item in failures)),
                         "all_attempted_latency": latencies(selected)})
    completion_all = usage["completion_tokens"]
    completion_passed = usage_summary(passed)["completion_tokens"]
    return {"name": name, "concurrency": phase["concurrency"], **counts, "source_wall_seconds": wall,
            "all_attempted_latency": all_latency, "passed_only_latency_recomputed": pass_latency,
            "raw_passed_only_summary": raw, "all_attempted_requests_per_second": len(attempted) / wall,
            "validated_requests_per_second": len(passed) / wall,
            "all_reported_completion_tokens_per_phase_wall_second": completion_all["sum_reported_values"] / wall
                if completion_all["all_attempts_reported"] else None,
            "validated_completion_tokens_per_phase_wall_second": completion_passed["sum_reported_values"] / wall
                if completion_passed["all_attempts_reported"] and passed else None,
            "reported_usage": usage, "per_case": per_case,
            "failures": [{key: sample.get(key) for key in ("case_id", "repetition", "status", "schema_valid", "behavior_valid", "failure_code", "latency_ms", "usage")} for sample in failed]}


def analyze(reports: dict, inputs: dict) -> dict:
    direct, gateway, system = (reports[key] for key in ("direct", "gateway", "system"))
    issues, paths = [], {}
    for label, report in (("direct", direct), ("gateway", gateway)):
        if report.get("schema_version") != "model-contract-benchmark-v2" or report.get("evidence_kind") != "live_model":
            issues.append(label + ": unexpected report schema or evidence kind")
        controls = report["measurement"]
        for key, expected in {"case_count": 6, "concurrency": [1, 2, 4], "repetitions": 5, "warmup_rounds": 1}.items():
            if controls.get(key) != expected:
                issues.append(label + ": unexpected measurement " + key)
        for field in ("deployment", "measurement"):
            content = report["deployment_manifest" if field == "deployment" else field]
            if report.get(field + "_sha256") != canonical_hash(content):
                issues.append(label + ": invalid " + field + " identity hash")
        if report.get("request_upper_bound") != 96:
            issues.append(label + ": request upper bound is not 96")
        if [phase.get("concurrency") for phase in report["phases"]] != [1, 2, 4] or report["warmup"].get("concurrency") != 1:
            issues.append(label + ": unexpected phase order/concurrency")
        phases = [phase_analysis(report["warmup"], label + "/warmup", 1, issues)]
        phases.extend(phase_analysis(phase, label + "/concurrency-" + str(phase["concurrency"]), 5, issues)
                      for phase in report["phases"])
        observed_status = "passed" if all(phase["failed"] == 0 and phase["not_run"] == 0 for phase in phases) else "failed"
        if report.get("status") != observed_status:
            issues.append(label + ": quality gate status does not match sample evidence")
        all_samples = [sample for phase in (report["warmup"], *report["phases"]) for sample in phase["samples"]]
        paths[label] = {"source_status": report["status"], "source_created_at": report["created_at"],
                        "target": report["target"], "runtime": report["runtime"],
                        "warmup": phases[0], "phases": phases[1:],
                        "attempted_including_warmup": sum(phase["attempted"] for phase in phases),
                        "passed_including_warmup": sum(phase["passed"] for phase in phases),
                        "failed_including_warmup": sum(phase["failed"] for phase in phases),
                        "measured_phase_wall_seconds": sum(phase["source_wall_seconds"] for phase in phases[1:]),
                        "all_phase_wall_seconds_including_warmup": sum(phase["source_wall_seconds"] for phase in phases),
                        "reported_usage_including_warmup": usage_summary(all_samples),
                        "source_limitations": report.get("limitations", [])}
    parity = {
        "same_deployment_manifest": direct["deployment_manifest"] == gateway["deployment_manifest"] == system.get("deployment_manifest"),
        "same_deployment_hash": direct["deployment_sha256"] == gateway["deployment_sha256"] == system.get("execution_contract", {}).get("deployment_sha256"),
        "same_measurement_and_controls": direct["measurement"] == gateway["measurement"],
        "same_measurement_hash": direct["measurement_sha256"] == gateway["measurement_sha256"],
        "same_client_runtime": direct["runtime"] == gateway["runtime"],
        "same_openai_protocol": direct["target"]["protocol"] == gateway["target"]["protocol"] == "openai",
        "same_required_auth_mode": direct["target"]["auth_mode"] == gateway["target"]["auth_mode"] == "required",
        "single_server_slot": direct["deployment_manifest"].get("server_parallel_slots") == system.get("server_properties", {}).get("total_slots") == 1,
        "source_harness_matches_system": direct["runtime"]["harness_sha256"] == system.get("source_sha256", {}).get("RAGs/rags/model_benchmark.py"),
        "runtime_matches_measurement": all(direct["runtime"].get(key) == direct["measurement"].get(key) for key in ("adapter_sha256", "harness_sha256")),
        "inference_recorded": system.get("execution_contract", {}).get("model_inference_executed") is True,
        "cache_disabled_in_execution_contract": system.get("execution_contract", {}).get("parameters", {}).get("prompt_cache") is False
            and system.get("execution_contract", {}).get("parameters", {}).get("cache_ram_mib") == 0,
    }
    for name, valid in parity.items():
        if not valid:
            issues.append("identity: " + name)
    total_attempted = sum(path["attempted_including_warmup"] for path in paths.values())
    warmup_attempted = sum(path["warmup"]["attempted"] for path in paths.values())
    measured_attempted = sum(phase["attempted"] for path in paths.values() for phase in path["phases"])
    cache_report_count = sum(path["reported_usage_including_warmup"]["cached_prompt_tokens"]["report_count"] for path in paths.values())
    cache_total = sum(path["reported_usage_including_warmup"]["cached_prompt_tokens"]["sum_reported_values"] or 0 for path in paths.values())
    if total_attempted != 192:
        issues.append("Expected exactly 192 attempted requests including 12 warmups")
    if system.get("benchmark_status") != {"direct": direct["status"], "gateway": gateway["status"]}:
        issues.append("System benchmark status does not match raw reports")
    if system.get("status") != "failed" or system.get("failure") != "generation_contract_benchmark_failed":
        issues.append("Expected preserved failed generation-contract system gate")
    if system.get("performance_comparison_status") != "not_generated_due_to_failed_contracts":
        issues.append("System did not preserve the failed comparison gate")
    failed_contracts = any(report.get("status") != "passed" for report in (direct, gateway))
    if not failed_contracts:
        issues.append("This diagnostic helper expects a failed generation-contract gate")
    return {
        "schema_version": "qwen-performance-descriptive-analysis-v1", "analysis_created_at": datetime.now(timezone.utc).isoformat(),
        "analysis_status": "complete" if not issues else "invalid_evidence", "status": "failed",
        "comparison_permitted": False, "speedup_computed": False,
        "comparison_block_reason": "Generation contracts failed; identity parity does not authorize a speedup claim.",
        "evidence_issues": issues, "raw_reports": inputs,
        "source_measured_times": {"system_measured_at": system.get("measured_at"), "system_elapsed_seconds": system.get("elapsed_seconds"),
                                  "direct_created_at": direct.get("created_at"), "gateway_created_at": gateway.get("created_at"),
                                  "per_phase_start_end_timestamps_recorded": False},
        "request_accounting": {"expected_formula": "2 * (6 warmup + 3 * 30 measured) = 192", "expected_attempted": 192,
                               "actual_attempted": total_attempted, "measured_attempted": measured_attempted,
                               "warmup_attempted": warmup_attempted, "complete": total_attempted == 192 and measured_attempted == 180 and warmup_attempted == 12,
                               "cached_token_report_count": cache_report_count, "cached_token_total": cache_total,
                               "all_192_reported_zero_cached_tokens": cache_report_count == total_attempted == 192 and cache_total == 0,
                               "passed_including_warmup": sum(path["passed_including_warmup"] for path in paths.values()),
                               "failed_including_warmup": sum(path["failed_including_warmup"] for path in paths.values())},
        "identity_checks": parity, "deployment_manifest": direct["deployment_manifest"], "measurement": direct["measurement"],
        "execution_contract": system.get("execution_contract"), "source_sha256_at_measurement": system.get("source_sha256"),
        "paths": paths,
        "system": {key: system.get(key) for key in ("status", "failure", "application_workflows_status", "benchmark_status", "performance_comparison_status", "runtime", "gateway_version", "host")},
        "memory": {"model_process": system.get("model_memory"), "gateway_process": system.get("gateway_memory"),
                   "child_peak_rss_bytes": system.get("child_peak_rss_bytes"),
                   "scope": "Shared processes sampled after both benchmark paths; VmHWM is lifetime process high water and VmRSS is a point observation. No per-path or per-concurrency attribution; child peak is not aggregate memory."},
        "limitations": [
            "Six synthetic contract cases, five repeats per case/concurrency; not a general model-quality benchmark.",
            "p95 uses only 30 attempted or 25 validated samples in each measured phase; no confidence interval or significance claim.",
            "Direct then gateway paths run sequentially; timing differences may reflect load, order and runtime state.",
            "One server slot: client concurrency 2/4 includes server queueing and does not establish parallel decoding.",
            "All-attempted latency retains behavioral failures; passed-only metrics condition on successful validation and may be biased.",
            "Non-streaming elapsed request/phase timings include prompt processing, generation, transport and validation; no TTFT or decoder-only token rate.",
            "Zero cached_prompt_tokens requires explicit zero usage in every sample; this does not prove all hardware/runtime caches are absent.",
            "Raw benchmark does not measure retrieval, embeddings, database latency or complete agent workflow throughput.",
            "Descriptive failed-run evidence only: no speedup, gateway performance gain, native-weight equivalence or production-readiness conclusion.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=WORKSPACE / "agentic-system/reports/qwen-live-performance-20261123")
    parser.add_argument("--output", type=Path, default=WORKSPACE / "qwen-performance-analysis-20261123.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a fresh scratch path")
    try:
        reports, inputs = {}, {}
        for label in ("direct", "gateway", "system"):
            reports[label], inputs[label] = read_report((args.input_dir / (label + ".json")).resolve())
        result = analyze(reports, inputs)
        for metadata in inputs.values():
            if hashlib.sha256(Path(metadata["path"]).read_bytes()).hexdigest() != metadata["sha256"]:
                result["evidence_issues"].append("Raw report changed during analysis")
                result["analysis_status"] = "invalid_evidence"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"analysis_status": "input_error", "error_type": type(error).__name__,
                          "comparison_permitted": False, "speedup_computed": False}), file=sys.stderr)
        return 2
    print(json.dumps({"analysis_status": result["analysis_status"], "status": result["status"],
                      "comparison_permitted": False, "attempted": result["request_accounting"]["actual_attempted"],
                      "evidence_issues": result["evidence_issues"], "output": str(args.output)}))
    return 0 if result["analysis_status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
