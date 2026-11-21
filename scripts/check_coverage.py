"""Enforce separate line/branch floors without omitting production modules."""
import argparse
import json
from pathlib import Path
import re
import tomllib


def check(report_path: Path, root: Path) -> dict:
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]
    settings = config["testing"]["coverage"]
    coverage_config = config.get("coverage", {})
    suppression_options = {"omit", "exclude_lines", "exclude_also", "partial_branches", "partial_always", "partial_also"}
    if any(suppression_options & set(section) for section in coverage_config.values() if isinstance(section, dict)):
        raise ValueError("Custom coverage omissions and exclusion rules are not allowed")
    baseline = json.loads((root / settings["baseline"]).read_text())
    report = json.loads(report_path.read_text())
    package = root / settings["package"]
    expected = {path.resolve() for path in package.rglob("*.py")}
    measured = {(root / name).resolve() for name in report["files"]}
    missing = sorted(str(path.relative_to(root)) for path in expected - measured)
    if missing:
        raise ValueError("Production files missing from coverage: " + ", ".join(missing))
    for path in expected:
        if re.search(r"#\s*pragma:\s*no\s*(?:cover|branch)\b", path.read_text(), re.IGNORECASE):
            raise ValueError("Production coverage suppression pragma: " + str(path.relative_to(root)))
    for name, item in report["files"].items():
        path = (root / name).resolve()
        if path not in expected:
            continue
        relative = path.relative_to(root).as_posix()
        lines = path.read_text().splitlines()
        excluded = item.get("excluded_lines", [])
        allowed_count = baseline.get("coverage_by_file", {}).get(relative, {}).get("excluded_lines", 0)
        if len(excluded) != item["summary"].get("excluded_lines", 0) or len(excluded) > allowed_count:
            raise ValueError("New or inconsistent coverage exclusions: " + relative)
        excluded_source = [lines[index - 1].strip() for index in excluded if lines[index - 1].strip()]
        if excluded_source != baseline.get("excluded_source", {}).get(relative, []):
            raise ValueError("Excluded production source differs from the baseline: " + relative)
    summaries = [item["summary"] for name, item in report["files"].items() if (root / name).resolve() in expected]
    statements = sum(item["num_statements"] for item in summaries)
    branches = sum(item["num_branches"] for item in summaries)
    if not statements or not branches:
        raise ValueError("Coverage must measure both production statements and branches")
    lines = 100 * sum(item["covered_lines"] for item in summaries) / statements
    branch = 100 * sum(item["covered_branches"] for item in summaries) / branches
    result = {"scope": "complete production Python package; ordinary parent-process unit+integration",
              "files": len(expected), "line_percent": lines, "branch_percent": branch,
              "minimum_lines": settings["minimum_lines"], "minimum_branches": settings["minimum_branches"],
              "passed": lines >= settings["minimum_lines"] and branch >= settings["minimum_branches"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/testing/coverage-core.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = Path(args.report)
    result = check(report if report.is_absolute() else root / report, root)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
