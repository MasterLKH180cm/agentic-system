"""Explicit test layers, inventory and fail-closed resource gates."""
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("AGENT_ENV", "test")

pytest_plugins = ["pytester"]
LAYERS = {"unit", "integration", "e2e"}
RESOURCES = {"external", "encoders", "research_raptor", "research_lightrag", "postgres", "redis", "s3", "browser"}
INVENTORY = pytest.StashKey[list]()
INCOMPLETE = pytest.StashKey[list]()


def pytest_addoption(parser):
    group = parser.getgroup("test evidence")
    group.addoption("--require-no-skips", action="store_true", help="Fail on any selected skip, xfail or xpass")
    group.addoption("--test-inventory", metavar="PATH", help="Write collected/selected test layers as JSON")


def pytest_configure(config):
    for name in sorted(LAYERS):
        config.addinivalue_line("markers", f"{name}: primary test layer (exactly one required)")
    for name in sorted(RESOURCES):
        config.addinivalue_line("markers", f"{name}: explicitly provisioned integration/E2E resource")
    config.stash[INVENTORY] = []
    config.stash[INCOMPLETE] = []


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    path = Path(__file__).parent / "tests" / "test_layers.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("tests"), dict):
        raise pytest.UsageError("Invalid explicit legacy test-layer manifest")
    legacy = manifest["tests"]
    for item in items:
        filename = item.path.relative_to(config.rootpath).as_posix()
        function = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        key = filename + "::" + function
        recorded = legacy.get(key)
        markers = {mark.name for mark in item.iter_markers()} & LAYERS
        if recorded:
            layer = recorded.get("layer")
            resources = recorded.get("resources")
            if layer not in LAYERS or not isinstance(resources, list) or not set(resources) <= RESOURCES:
                raise pytest.UsageError("Invalid test-layer entry: " + key)
            if markers and markers != {layer}:
                raise pytest.UsageError("Explicit marker conflicts with legacy classification: " + item.nodeid)
            item.add_marker(getattr(pytest.mark, layer))
            for resource in resources:
                item.add_marker(getattr(pytest.mark, resource))
        markers = {mark.name for mark in item.iter_markers()} & LAYERS
        if len(markers) != 1:
            raise pytest.UsageError("Exactly one unit/integration/e2e layer is required: " + item.nodeid)
        layer = next(iter(markers))
        resources = sorted({mark.name for mark in item.iter_markers()} & RESOURCES)
        if resources and layer == "unit":
            raise pytest.UsageError("Resource-dependent tests cannot be unit tests: " + item.nodeid)
        item.user_properties.extend([("layer", layer), ("resources", ",".join(resources))])
        config.stash[INVENTORY].append({"nodeid": item.nodeid, "layer": layer, "resources": resources})


def pytest_collection_finish(session):
    destination = session.config.getoption("--test-inventory")
    if not destination:
        return
    selected = {item.nodeid for item in session.items}
    rows = [{**row, "selected": row["nodeid"] in selected} for row in session.config.stash[INVENTORY]]
    output = Path(destination)
    if not output.is_absolute():
        output = session.config.rootpath / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version": "test-inventory-v1", "collected": len(rows),
                                  "selected": len(selected), "deselected": len(rows) - len(selected),
                                  "tests": rows}, indent=2) + "\n", encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if report.skipped or hasattr(report, "wasxfail"):
        kind = "xfail/xpass" if hasattr(report, "wasxfail") else "skipped"
        entry = (item.nodeid, kind)
        if entry not in item.config.stash[INCOMPLETE]:
            item.config.stash[INCOMPLETE].append(entry)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # Collection-time importorskip/skip never reaches runtest_makereport.
    result = yield
    report = result.get_result()
    if report.skipped:
        collector.config.stash[INCOMPLETE].append((report.nodeid, "collection skipped"))


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--require-no-skips") and session.config.stash[INCOMPLETE]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, config):
    incomplete = config.stash[INCOMPLETE]
    if config.getoption("--require-no-skips") and incomplete:
        terminalreporter.section("required test resource gate failed", red=True)
        for nodeid, kind in incomplete:
            terminalreporter.write_line(f"{nodeid}: {kind}; selected cases must execute", red=True)
