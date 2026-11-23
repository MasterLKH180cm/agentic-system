"""Synthetic diagnostics preserve application results and exclude private metadata."""
import asyncio
import builtins
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import qwen_live_worker

pytestmark = pytest.mark.unit


def test_response_evidence_allowlists_fields_without_mutating_the_response():
    payload = {
        "choices": [
            {"message": {"content": "Public synthetic answer", "refusal": None,
                         "tool_calls": [{"arguments": "PRIVATE-TOOL-ARGUMENT"}],
                         "provider_metadata": "PRIVATE-MESSAGE-METADATA"},
             "finish_reason": "stop", "logprobs": "PRIVATE-LOGPROBS"},
            {"message": {"content": "PRIVATE-SECOND-CHOICE"}},
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 10**9, "total_tokens": 42,
                  "prompt_tokens_details": {"api_key": "PRIVATE-USAGE-METADATA"}},
        "headers": {"Authorization": "Bearer PRIVATE-CREDENTIAL"},
        "request": {"url": "https://PRIVATE-ENDPOINT", "body": "PRIVATE-PROMPT"},
        "provider_metadata": "PRIVATE-PROVIDER-METADATA",
    }
    original = deepcopy(payload)
    result = qwen_live_worker.response_evidence(payload)
    assert result == {
        "content": "Public synthetic answer", "content_truncated": False,
        "refusal": None, "finish_reason": "stop",
        "usage": {"prompt_tokens": 0, "completion_tokens": 10**9, "total_tokens": 42},
    }
    assert "PRIVATE-" not in json.dumps(result)
    assert payload == original


@pytest.mark.parametrize("field", ["content", "refusal"])
@pytest.mark.parametrize("text,expected,truncated", [
    ("", "", False),
    ("x" * 16384, "x" * 16384, False),
    ("x" * 16385, "x" * 16384, True),
    ("界" * 5462, "界" * 5461, True),
    ("🙂" * 4096, "🙂" * 4096, False),
    ("🙂" * 4097, "🙂" * 4096, True),
])
def test_response_text_is_bounded_in_utf8_bytes_without_broken_characters(field, text, expected, truncated):
    result = qwen_live_worker.response_evidence({"choices": [{"message": {field: text}}]})
    assert result[field] == expected
    assert result[field + "_truncated"] is truncated
    assert len(result[field].encode("utf-8")) <= 16384


@pytest.mark.parametrize("value", [-1, 10**9 + 1, True, False, 1.0, "2", None, [], {}])
def test_response_usage_rejects_noninteger_or_out_of_bounds_counters(value):
    result = qwen_live_worker.response_evidence({"usage": {
        "prompt_tokens": value, "completion_tokens": value, "total_tokens": value,
        "arbitrary": 5,
    }})
    assert result == {"usage": {}}


@pytest.mark.parametrize("payload", [
    None, [], "not an object", {}, {"choices": None}, {"choices": []}, {"choices": [None]},
    {"choices": [{"message": []}]},
    {"choices": [{"message": {"content": {"private": "nested"}, "refusal": ["private"]}}]},
])
def test_response_evidence_ignores_invalid_shapes_and_nested_values(payload):
    assert qwen_live_worker.response_evidence(payload) == {}


def test_finish_reason_has_a_separate_short_bound():
    result = qwen_live_worker.response_evidence({"choices": [{"finish_reason": "🙂" * 81}]})
    assert result == {"finish_reason": "🙂" * 80}
    assert len(result["finish_reason"].encode("utf-8")) <= 320


def test_exception_evidence_excludes_message_source_and_private_paths():
    def descend(depth):
        if depth:
            return descend(depth - 1)
        raise RuntimeError("PRIVATE-KEY https://PRIVATE-ENDPOINT PRIVATE-USER-CONTENT")

    descend.__code__ = descend.__code__.replace(co_filename="/PRIVATE-DIRECTORY/provider.py")
    try:
        descend(10)
    except RuntimeError as exc:
        result = qwen_live_worker.exception_evidence(exc)
    assert result["error_type"] == "RuntimeError"
    assert len(result["error_location"]) == 6
    assert all(set(frame) == {"module", "line", "function"} for frame in result["error_location"])
    assert all(frame["module"] == "provider.py" for frame in result["error_location"])
    assert "PRIVATE-" not in json.dumps(result)


def test_exception_evidence_does_not_format_the_exception():
    class OpaqueError(Exception):
        def __str__(self):
            raise AssertionError("Exception messages must not be formatted")

    assert qwen_live_worker.exception_evidence(OpaqueError("PRIVATE-MESSAGE")) == {
        "error_type": "OpaqueError", "error_location": [],
    }


def test_http_exception_retains_only_status_without_response_request_or_message():
    request = httpx.Request("POST", "https://PRIVATE-ENDPOINT/completions",
                            headers={"Authorization": "Bearer PRIVATE-CREDENTIAL"}, content=b"PRIVATE-PROMPT")
    response = httpx.Response(503, request=request, json={"error": "PRIVATE-PROVIDER-BODY"},
                              headers={"private-provider-metadata": "PRIVATE-METADATA"})
    result = qwen_live_worker.exception_evidence(httpx.HTTPStatusError(
        "PRIVATE-ERROR-MESSAGE", request=request, response=response,
    ))
    assert result == {"error_type": "HTTPStatusError", "error_location": [], "http_status": 503}
    assert "PRIVATE-" not in json.dumps(result)


@pytest.mark.parametrize("environment,identities", [
    (None, {"PRIVATE-KEY": "qwen-live-synthetic"}),
    ("production", {"PRIVATE-KEY": "qwen-live-synthetic"}),
    ("development", {"PRIVATE-KEY": "qwen-live-synthetic"}),
    ("TEST", {"PRIVATE-KEY": "qwen-live-synthetic"}),
    ("test", {}),
    ("test", {"PRIVATE-KEY": "another-tenant"}),
    ("test", {"PRIVATE-KEY": "qwen-live-synthetic", "SECOND-KEY": "another-tenant"}),
])
def test_preflight_rejects_non_test_or_non_synthetic_tenants_before_imports(
    monkeypatch, tmp_path, capsys, environment, identities,
):
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setattr(sys, "argv", ["qwen_live_worker.py", "--synthetic-trace", str(trace)])
    if environment is None:
        monkeypatch.delenv("AGENT_ENV", raising=False)
    else:
        monkeypatch.setenv("AGENT_ENV", environment)
    monkeypatch.setenv("AGENT_KEYS_JSON", json.dumps(identities))
    original_import = builtins.__import__
    attempted = []

    def reject_worker_import(name, *args, **kwargs):
        if name in {"agentic.model", "agentic.tools", "agentic.worker"}:
            attempted.append(name)
            raise AssertionError("Preflight must reject before importing the worker")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_worker_import)
    with pytest.raises(SystemExit) as caught:
        qwen_live_worker.main()
    assert caught.value.code == 2
    assert attempted == []
    assert not trace.exists()
    assert "PRIVATE-KEY" not in capsys.readouterr().err


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Replace imported worker/model/tool modules; no real worker is ever created."""
    state = SimpleNamespace(
        calls=[], replies=[], result=object(), failure=None, tool_failure=None, operation=lambda: None,
        reply=SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"message": {"content": "Public synthetic answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }),
    )

    class Gateway:
        @staticmethod
        async def _request(*args, **kwargs):
            return state.reply

        async def complete(self, system, message, **kwargs):
            state.calls.append((system, message, kwargs))
            if state.failure is not None:
                raise state.failure
            reply = await self._request("client", "route", b"request", kwargs.get("run_id"), False)
            state.replies.append(reply)
            return state.result

    class Tools:
        async def execute(self, job, inputs):
            if state.tool_failure is not None:
                raise state.tool_failure
            return state.result

    state.gateway, state.tools = Gateway, Tools
    state.trace = tmp_path / "trace.jsonl"
    state.originals = (Gateway._request, Gateway.complete, Tools.execute)
    monkeypatch.setitem(sys.modules, "agentic.model", SimpleNamespace(ModelGateway=Gateway))
    monkeypatch.setitem(sys.modules, "agentic.tools", SimpleNamespace(Tools=Tools))
    monkeypatch.setitem(sys.modules, "agentic.worker", SimpleNamespace(main=lambda: state.operation()))
    monkeypatch.setattr(sys, "argv", ["qwen_live_worker.py", "--synthetic-trace", str(state.trace)])
    monkeypatch.setenv("AGENT_ENV", "test")
    monkeypatch.setenv("AGENT_KEYS_JSON", '{"PRIVATE-CREDENTIAL":"qwen-live-synthetic"}')
    return state


def invoke_model(launcher, **kwargs):
    return asyncio.run(launcher.gateway().complete("PRIVATE-SYSTEM", "PRIVATE-PROMPT", **kwargs))


def test_capture_preserves_response_arguments_and_restores_class_methods(launcher):
    schema = {"type": "object"}
    kwargs = {"tenant": "qwen-live-synthetic", "run_id": "synthetic-run",
              "purpose": "planning", "response_schema": schema}
    launcher.operation = lambda: invoke_model(launcher, **kwargs)
    qwen_live_worker.main()
    assert launcher.calls == [("PRIVATE-SYSTEM", "PRIVATE-PROMPT", kwargs)]
    assert launcher.calls[0][2]["response_schema"] is schema
    assert launcher.replies == [launcher.reply]
    assert (launcher.gateway._request, launcher.gateway.complete, launcher.tools.execute) == launcher.originals
    records = [json.loads(line) for line in launcher.trace.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["event"] == "model_response"
    assert records[0]["purpose"] == "planning"
    assert records[0]["capture_failures"] == 0
    assert len(records[0]["prompt_sha256"]) == len(records[0]["schema_sha256"]) == 64
    assert "PRIVATE-" not in json.dumps(records)
    assert launcher.trace.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["parse", "encode", "serialize", "write", "flush"])
def test_diagnostic_failure_never_changes_the_original_response(
    launcher, monkeypatch, capsys, fault,
):
    expected = launcher.result
    original_dumps = json.dumps
    if fault == "parse":
        def bad_json():
            raise ValueError("PRIVATE-PARSE-DETAIL")
        launcher.reply.json = bad_json
    elif fault == "encode":
        launcher.reply.json = lambda: {"choices": [{"message": {"content": "\ud800"}}]}
    elif fault == "serialize":
        def bad_dumps(value, **kwargs):
            if isinstance(value, dict) and value.get("event"):
                raise ValueError("PRIVATE-SERIALIZATION-DETAIL")
            return original_dumps(value, **kwargs)
        monkeypatch.setattr(qwen_live_worker.json, "dumps", bad_dumps)
    else:
        class BrokenOutput:
            def write(self, text):
                if fault == "write":
                    raise OSError("PRIVATE-WRITE-DETAIL")

            def flush(self):
                if fault == "flush":
                    raise OSError("PRIVATE-FLUSH-DETAIL")

            def close(self):
                pass

        monkeypatch.setattr(Path, "open", lambda *args, **kwargs: BrokenOutput())
        monkeypatch.setattr(qwen_live_worker.os, "chmod", lambda *args: None)

    returned = []
    launcher.operation = lambda: returned.append(invoke_model(launcher))
    qwen_live_worker.main()
    assert returned == [expected]
    assert launcher.replies == [launcher.reply]
    assert len(launcher.calls) == 1
    assert capsys.readouterr().err == "QWEN_CAPTURE_FAILURE count=1\n"


@pytest.mark.parametrize("stage", ["model", "tool"])
def test_diagnostic_and_close_failures_do_not_replace_the_original_exception(
    launcher, monkeypatch, capsys, stage,
):
    original = RuntimeError("PRIVATE-ORIGINAL-ERROR")

    class BrokenOutput:
        def write(self, text):
            raise OSError("PRIVATE-DIAGNOSTIC-ERROR")

        def close(self):
            raise OSError("PRIVATE-CLOSE-ERROR")

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: BrokenOutput())
    monkeypatch.setattr(qwen_live_worker.os, "chmod", lambda *args: None)
    if stage == "model":
        launcher.failure = original
        launcher.operation = lambda: invoke_model(launcher)
    else:
        launcher.tool_failure = original
        launcher.operation = lambda: asyncio.run(launcher.tools().execute(
            {"run_id": "synthetic-run", "tool": "model.summarize"}, {},
        ))
    with pytest.raises(RuntimeError) as caught:
        qwen_live_worker.main()
    assert caught.value is original
    assert (launcher.gateway._request, launcher.gateway.complete, launcher.tools.execute) == launcher.originals
    assert capsys.readouterr().err == (
        "QWEN_CAPTURE_FAILURE count=1\nQWEN_CAPTURE_FAILURE count=2\n"
    )


def test_next_successful_record_reports_prior_capture_failures(launcher, monkeypatch, capsys):
    original = qwen_live_worker.response_evidence
    attempts = 0

    def once_broken(payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("PRIVATE-EXTRACTION-DETAIL")
        return original(payload)

    monkeypatch.setattr(qwen_live_worker, "response_evidence", once_broken)
    launcher.operation = lambda: [invoke_model(launcher) for _ in range(2)]
    qwen_live_worker.main()
    records = [json.loads(line) for line in launcher.trace.read_text().splitlines()]
    assert len(launcher.calls) == 2
    assert len(records) == 1 and records[0]["capture_failures"] == 1
    assert capsys.readouterr().err == "QWEN_CAPTURE_FAILURE count=1\n"


@pytest.mark.parametrize("flush_fails", [False, True])
def test_capture_record_limit_counts_writes_even_when_flush_fails(launcher, monkeypatch, capsys, flush_fails):
    written = []

    class Output:
        def write(self, text):
            written.append(text)

        def flush(self):
            if flush_fails:
                raise OSError("PRIVATE-FLUSH-DETAIL")

        def close(self):
            pass

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Output())
    monkeypatch.setattr(qwen_live_worker.os, "chmod", lambda *args: None)
    launcher.operation = lambda: [invoke_model(launcher) for _ in range(140)]
    qwen_live_worker.main()
    assert len(launcher.calls) == 140
    assert len(written) == 128
    assert all(json.loads(line)["event"] == "model_response" for line in written)
    diagnostic = capsys.readouterr().err
    if flush_fails:
        assert diagnostic.splitlines()[-1] == "QWEN_CAPTURE_FAILURE count=128"
        assert len(diagnostic.splitlines()) == 128
    else:
        assert diagnostic == ""


def test_context_hashing_and_unavailable_stderr_cannot_change_a_model_result(launcher, monkeypatch):
    class BrokenErrorStream:
        def write(self, text):
            raise OSError("PRIVATE-STDERR-DETAIL")

        def flush(self):
            raise OSError("PRIVATE-STDERR-DETAIL")

    monkeypatch.setattr(qwen_live_worker.sys, "stderr", BrokenErrorStream())
    # The real model owns schema validation. Diagnostic hashing must not preempt it.
    schema = {"not_json": object()}
    returned = []
    launcher.operation = lambda: returned.append(invoke_model(launcher, response_schema=schema))
    qwen_live_worker.main()
    assert returned == [launcher.result]
    assert launcher.calls[0][2]["response_schema"] is schema
    record, = [json.loads(line) for line in launcher.trace.read_text().splitlines()]
    assert record["event"] == "model_response" and record["capture_failures"] == 1


@pytest.fixture
def collect_trace(monkeypatch):
    # Collector tests need no PostgreSQL extra or live-harness service imports.
    # None of these collaborators is used by the pure file collector.
    dependencies = {
        "psycopg": (),
        "check_litellm": ("AcceptanceFailure", "environment", "free_port", "load_module", "read_env",
                          "require", "stop_process", "temporary_gateway_database"),
        "qwen_acceptance": ("ARCHIVE_TEXT", "validate_rag_generation", "validate_workflow"),
    }
    for name, fields in dependencies.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**dict.fromkeys(fields)))
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_qwen_live.py"
    spec = importlib.util.spec_from_file_location("qwen_trace_collector_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_synthetic_trace


def test_trace_collector_absence_preserves_the_application_result(collect_trace, tmp_path):
    report = {"status": "failed", "failure": "actual_acceptance_failure"}
    collect_trace(report, tmp_path)
    assert report == {
        "status": "failed", "failure": "actual_acceptance_failure",
        "synthetic_trace_capture": {"status": "unavailable", "reported_capture_failures": 0},
    }


def test_trace_collector_reads_complete_records_and_only_fixed_diagnostic_counters(collect_trace, tmp_path):
    record = {"event": "model_response", "content": "Public synthetic answer", "capture_failures": 0}
    (tmp_path / "worker-trace.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "worker.log").write_text(
        "PRIVATE-WORKER-LOG https://PRIVATE-ENDPOINT\n"
        "QWEN_CAPTURE_FAILURE count=2\nQWEN_CAPTURE_FAILURE count=7\n"
        "QWEN_CAPTURE_FAILURE count=-1\nQWEN_CAPTURE_FAILURE count=invalid\n"
        "QWEN_CAPTURE_FAILURE count=4\n",
        encoding="utf-8",
    )
    report = {"status": "passed"}
    collect_trace(report, tmp_path)
    assert report["status"] == "passed"
    assert report["synthetic_worker_trace"] == [record]
    assert report["synthetic_trace_capture"] == {"status": "incomplete", "reported_capture_failures": 7}
    assert "PRIVATE-" not in json.dumps(report)


@pytest.mark.parametrize("data,error_type", [
    (b'{"event":"model_response"}\n{"PRIVATE-PARTIAL":', "JSONDecodeError"),
    (b"\xffPRIVATE-NON-UTF8", "UnicodeDecodeError"),
])
def test_trace_collector_malformed_trace_never_replaces_the_actual_failure(
    collect_trace, tmp_path, data, error_type,
):
    (tmp_path / "worker-trace.jsonl").write_bytes(data)
    report = {"status": "failed", "failure": "actual_acceptance_failure"}
    collect_trace(report, tmp_path)
    assert report["status"] == "failed" and report["failure"] == "actual_acceptance_failure"
    assert report["synthetic_trace_capture"] == {
        "status": "incomplete", "reported_capture_failures": 0, "error_type": error_type,
    }
    assert "synthetic_worker_trace" not in report
    assert "PRIVATE-" not in json.dumps(report)


def test_trace_collector_rejects_too_many_records(collect_trace, tmp_path):
    (tmp_path / "worker-trace.jsonl").write_text("{}\n" * 129, encoding="utf-8")
    report = {"status": "passed"}
    collect_trace(report, tmp_path)
    assert report["status"] == "passed"
    assert report["synthetic_trace_capture"] == {
        "status": "incomplete", "reported_capture_failures": 0, "failure": "trace_record_limit",
    }
    assert "synthetic_worker_trace" not in report


def test_trace_collector_rejects_oversized_file_before_parsing(collect_trace, tmp_path):
    with (tmp_path / "worker-trace.jsonl").open("wb") as output:
        output.truncate(32 * 1024 * 1024 + 1)
    report = {"status": "failed", "failure": "actual_acceptance_failure"}
    collect_trace(report, tmp_path)
    assert report["failure"] == "actual_acceptance_failure"
    assert report["synthetic_trace_capture"] == {
        "status": "incomplete", "reported_capture_failures": 0, "failure": "trace_size_limit",
    }
    assert "synthetic_worker_trace" not in report


def test_trace_collector_unreadable_trace_does_not_echo_path_or_error_message(collect_trace, tmp_path, monkeypatch):
    trace = tmp_path / "worker-trace.jsonl"
    trace.touch()
    original_open = Path.open

    def deny_trace(path, *args, **kwargs):
        if path == trace:
            raise PermissionError("PRIVATE-FILESYSTEM-LOCATION")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_trace)
    report = {"status": "passed"}
    collect_trace(report, tmp_path)
    assert report["status"] == "passed"
    assert report["synthetic_trace_capture"] == {
        "status": "incomplete", "reported_capture_failures": 0, "error_type": "PermissionError",
    }
    assert "PRIVATE-" not in json.dumps(report)
