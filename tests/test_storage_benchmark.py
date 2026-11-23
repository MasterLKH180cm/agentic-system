"""Benchmark statistics reject wrong results and measure validated samples."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def harness():
    spec = importlib.util.spec_from_file_location("storage_benchmark", ROOT / "scripts/benchmark_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentiles_include_all_successful_samples_and_interpolate():
    module = harness()
    assert module.percentile([8, 2, 4, 6], 50) == 5
    assert module.percentile([2, 4, 6, 8], 95) == pytest.approx(7.7)
    assert module.percentile([11], 99) == 11


def test_wrong_result_fails_benchmark_instead_of_reporting_success():
    def invalid(_):
        raise AssertionError("result parity failed")

    with pytest.raises(AssertionError, match="result parity"):
        harness().measure(invalid, 10, 2)


def test_measurement_counts_every_completed_operation():
    seen = []
    result = harness().measure(lambda number: seen.append(number), 12, 3)
    assert sorted(seen) == list(range(12))
    assert result["samples"] == result["validated_results"] == 12
    assert result["errors"] == 0 and result["concurrency"] == 3
    assert result["throughput_per_second"] == 12 / result["elapsed_seconds"]
    assert 0 <= result["p50_ms"] <= result["p95_ms"] <= result["p99_ms"]
