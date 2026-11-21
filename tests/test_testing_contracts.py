"""Negative controls for CI's layer, resource and coverage evidence gates."""
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_suite(pytester):
    pytester.makeconftest((ROOT / "conftest.py").read_text())
    (pytester.path / "tests").mkdir()
    (pytester.path / "tests/test_layers.json").write_text(json.dumps({"schema_version": 1, "tests": {}}))
    pytester.makeini("[pytest]\naddopts = --strict-markers --strict-config\njunit_family = xunit2")
    return pytester


@pytest.mark.parametrize("marker", ["", "@pytest.mark.unit\n@pytest.mark.integration\n"])
def test_missing_or_conflicting_primary_layers_fail_collection(isolated_suite, marker):
    isolated_suite.makepyfile("import pytest\n" + marker + "def test_case():\n    assert True\n")
    result = isolated_suite.runpytest_subprocess("-q", "--require-no-skips")
    assert result.ret != 0
    assert "Exactly one unit/integration/e2e layer is required" in result.stderr.str()


@pytest.mark.parametrize("body", [
    "pytest.skip('required runtime missing')",
    "pytest.xfail('known defect')",
])
def test_selected_runtime_skip_or_xfail_fails_the_gate(isolated_suite, body):
    isolated_suite.makepyfile("import pytest\n@pytest.mark.integration\ndef test_case():\n    " + body)
    result = isolated_suite.runpytest_subprocess("-q", "--require-no-skips")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required test resource gate failed*"])


def test_unexpected_xpass_cannot_turn_a_required_gate_green(isolated_suite):
    isolated_suite.makepyfile("""
        import pytest
        @pytest.mark.integration
        @pytest.mark.xfail(reason="a stale expected failure", strict=False)
        def test_case():
            assert True
    """)
    result = isolated_suite.runpytest_subprocess("-q", "--require-no-skips")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*xfail/xpass; selected cases must execute*"])


def test_collection_skip_cannot_hide_a_required_resource(isolated_suite):
    isolated_suite.makepyfile(test_missing="import pytest\npytest.skip('missing driver', allow_module_level=True)")
    isolated_suite.makepyfile(test_present="import pytest\n@pytest.mark.unit\ndef test_case():\n    assert True")
    result = isolated_suite.runpytest_subprocess("-q", "--require-no-skips")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*collection skipped; selected cases must execute*"])


def test_inventory_and_junit_distinguish_deselected_resources_from_skips(isolated_suite):
    isolated_suite.makepyfile("""
        import pytest
        @pytest.mark.unit
        @pytest.mark.parametrize("value", [1, 2])
        def test_small(value):
            assert value > 0
        @pytest.mark.integration
        @pytest.mark.external
        @pytest.mark.postgres
        def test_database():
            pytest.skip("database intentionally not provisioned in unit job")
    """)
    result = isolated_suite.runpytest_subprocess(
        "-q", "-m", "unit", "--require-no-skips", "--test-inventory", "inventory.json",
        "--junitxml", "junit.xml",
    )
    result.assert_outcomes(passed=2, deselected=1)
    assert result.ret == 0
    inventory = json.loads((isolated_suite.path / "inventory.json").read_text())
    assert (inventory["collected"], inventory["selected"], inventory["deselected"]) == (3, 2, 1)
    assert len({row["nodeid"] for row in inventory["tests"]}) == 3
    database = next(row for row in inventory["tests"] if not row["selected"])
    assert database["layer"] == "integration"
    assert database["resources"] == ["external", "postgres"]
    cases = ET.parse(isolated_suite.path / "junit.xml").findall(".//testcase")
    assert len(cases) == 2
    for case in cases:
        assert case.find("./properties/property[@name='layer']").get("value") == "unit"


def test_unit_layer_cannot_claim_real_service_dependencies(isolated_suite):
    isolated_suite.makepyfile("""
        import pytest
        @pytest.mark.unit
        @pytest.mark.redis
        def test_case():
            assert True
    """)
    result = isolated_suite.runpytest_subprocess("-q")
    assert result.ret != 0
    assert "Resource-dependent tests cannot be unit tests" in result.stderr.str()


def test_required_options_work_when_inventory_already_exists(isolated_suite):
    isolated_suite.makepyfile("import pytest\n@pytest.mark.unit\ndef test_case():\n    assert True")
    inventory = isolated_suite.path / "inventory.json"
    inventory.write_text('{"stale": true}')
    result = isolated_suite.runpytest_subprocess(
        "-q", "--require-no-skips", "--test-inventory", str(inventory),
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert json.loads(inventory.read_text())["selected"] == 1


@pytest.fixture
def coverage_gate(tmp_path):
    spec = importlib.util.spec_from_file_location("coverage_gate", ROOT / "scripts/check_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "production").mkdir()
    (tmp_path / "production/first.py").write_text("def first():\n    return 1\n")
    (tmp_path / "production/second.py").write_text("def second():\n    return 2\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.testing.coverage]\nbaseline = "baseline.json"\n'
        'package = "production"\nminimum_lines = 80\nminimum_branches = 75\n'
    )
    (tmp_path / "baseline.json").write_text('{"coverage_by_file": {}, "excluded_source": {}}')
    return module.check, tmp_path


def _coverage_report(root, *, covered_lines=80, covered_branches=75):
    summary = {"num_statements": 100, "num_branches": 100,
               "covered_lines": covered_lines, "covered_branches": covered_branches}
    report = {"files": {name: {"summary": summary.copy()} for name in
                        ("production/first.py", "production/second.py")}}
    path = root / "coverage.json"
    path.write_text(json.dumps(report))
    return path


@pytest.mark.parametrize("lines,branches,passed", [(80, 75, True), (79, 100, False), (100, 74, False)])
def test_separate_coverage_floors_cannot_mask_each_other(coverage_gate, lines, branches, passed):
    check, root = coverage_gate
    result = check(_coverage_report(root, covered_lines=lines, covered_branches=branches), root)
    assert result["passed"] is passed
    assert result["line_percent"] == lines
    assert result["branch_percent"] == branches


def test_coverage_report_cannot_omit_a_production_module(coverage_gate):
    check, root = coverage_gate
    path = _coverage_report(root)
    report = json.loads(path.read_text())
    del report["files"]["production/second.py"]
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Production files missing.*second.py"):
        check(path, root)


def test_line_only_coverage_does_not_satisfy_the_branch_gate(coverage_gate):
    check, root = coverage_gate
    path = _coverage_report(root)
    report = json.loads(path.read_text())
    for file in report["files"].values():
        file["summary"]["num_branches"] = 0
        file["summary"]["covered_branches"] = 0
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="both production statements and branches"):
        check(path, root)


@pytest.mark.parametrize("pragma", ["no cover", "no branch"])
def test_suppression_pragmas_cannot_inflate_coverage(coverage_gate, pragma):
    check, root = coverage_gate
    (root / "production/first.py").write_text("def first():  # pragma: " + pragma + "\n    return 1\n")
    with pytest.raises(ValueError, match="suppression pragma"):
        check(_coverage_report(root), root)


@pytest.mark.parametrize("option", ["exclude_lines", "exclude_also", "partial_branches", "partial_also", "omit"])
def test_custom_coverage_exclusion_configuration_is_rejected(coverage_gate, option):
    check, root = coverage_gate
    with (root / "pyproject.toml").open("a") as output:
        output.write('\n[tool.coverage.report]\n' + option + ' = ["unexecuted"]\n')
    with pytest.raises(ValueError, match="Custom coverage omissions"):
        check(_coverage_report(root), root)


def test_default_exclusions_must_match_the_recorded_source(coverage_gate):
    check, root = coverage_gate
    path = _coverage_report(root)
    report = json.loads(path.read_text())
    report["files"]["production/first.py"]["excluded_lines"] = [1]
    report["files"]["production/first.py"]["summary"]["excluded_lines"] = 1
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="New or inconsistent coverage exclusions"):
        check(path, root)


def test_existing_exclusion_cannot_be_moved_onto_different_code(coverage_gate):
    check, root = coverage_gate
    path = _coverage_report(root)
    report = json.loads(path.read_text())
    report["files"]["production/first.py"]["excluded_lines"] = [1]
    report["files"]["production/first.py"]["summary"]["excluded_lines"] = 1
    path.write_text(json.dumps(report))
    (root / "baseline.json").write_text(json.dumps({
        "coverage_by_file": {"production/first.py": {"excluded_lines": 1}},
        "excluded_source": {"production/first.py": ["def protocol_declaration(): ..."]},
    }))
    with pytest.raises(ValueError, match="Excluded production source differs"):
        check(path, root)
