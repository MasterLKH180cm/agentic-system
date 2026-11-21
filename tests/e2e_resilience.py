"""Real HTTP/worker failure recovery, cancellation, retries and SSE reconnect.

AGENT_E2E_POSTGRES_URL may select a disposable PostgreSQL database. No SQL state
is edited: leases expire on the real clock and the real worker is killed.
"""
import argparse
import json
import os
import signal
import time

from e2e_support import Services, eventually, require, verify_mcp, write_report


EXPECTED = ["worker_crash_recovery", "cancel_late_result_fencing", "sse_cursor_reconnect",
            "transient_http_retry", "retry_exhaustion", "persisted_step_deadline"]


def source_steps(*, arithmetic=False, publish=False):
    steps = []
    if arithmetic:
        steps.append({"id": "calc", "tool": "math.calculate", "args": {"expression": "6 * 7"}})
    steps.append({"id": "read", "tool": "rag.search", "depends_on": ["calc"] if arithmetic else []})
    steps.append({"id": "publish" if publish else "answer",
                  "tool": "artifact.publish" if publish else "model.summarize", "depends_on": ["read"]})
    return steps


def step(state, name):
    return next(item for item in state["steps"] if item["id"] == name)


def sse_events(lines):
    event = {}
    for line in lines:
        if not line:
            if event:
                yield event
                event = {}
            continue
        if line.startswith(":"):
            continue
        key, separator, value = line.partition(":")
        if separator:
            event[key] = value.lstrip()


def run(services, checks):
    question = "What is the session timeout after worker recovery?"
    fault = services.proxy.fault(question, hold_requests=1)
    run_id = services.submit(question, steps=source_steps(arithmetic=True))
    eventually(lambda: fault.forwarded == 1, description="actual RAG request held before worker crash")
    before = services.state(run_id)
    require(step(before, "calc")["status"] == "completed", "Dependency did not complete before crash")
    require(step(before, "read")["attempts"] == 1, "First lease was not claimed")
    deadline = step(before, "read")["deadline_at"]
    os.kill(services.worker.pid, signal.SIGKILL)
    services.worker.wait(timeout=4)
    services.worker = services.start_worker("worker-after-crash")
    state = services.finish(run_id)
    require(state["status"] == "completed", "Restarted worker did not complete recovery")
    require(step(state, "calc")["attempts"] == 1, "Committed dependency was replayed")
    require(step(state, "read")["attempts"] == 2 and fault.calls == 2, "Read was not recovered exactly once")
    require(step(state, "read")["deadline_at"] == deadline, "Recovery reset the absolute deadline")
    require("30 minutes" in step(state, "answer")["result"]["answer"], "Recovered answer lacks real source evidence")
    events = services.events(run_id)
    require(sum(event["type"] == "lease_expired" for event in events) == 1, "Lease recovery was not persisted")
    require([event["payload"]["attempt"] for event in events
             if event["type"] == "started" and event["step_id"] == "read"] == [1, 2], "Attempt events disagree")
    fault.release.set()
    eventually(lambda: fault.finished == 2, description="old and recovered proxy request cleanup")
    verify_mcp(services, run_id, "completed")
    checks.append({"id": EXPECTED[0], "passed": True, "read_attempts": 2, "committed_dependency_attempts": 1,
                   "deadline_preserved": True, "http_and_mcp_agree": True})

    question = "What is the session timeout during cancellation?"
    fault = services.proxy.fault(question, hold_requests=1)
    run_id = services.submit(question, steps=source_steps(publish=True))
    eventually(lambda: fault.forwarded == 1, description="actual in-flight cancellation request")
    url = services.agent_url + f"/workflows/{run_id}/events/stream"
    headers = {"Authorization": "Bearer " + services.key}
    with services.client.stream("GET", url, headers=headers, timeout=3) as response:
        require(response.status_code == 200, "Initial SSE connection failed")
        first = next(event for event in sse_events(response.iter_lines()) if event.get("event") == "workflow")
        cursor = int(first["id"])
    services.request("POST", f"/workflows/{run_id}/cancel")
    fault.release.set()
    eventually(lambda: fault.finished == 1, description="late response released after cancellation")
    # Single-worker concurrency makes this sentinel a barrier after the old task.
    sentinel = services.finish(services.submit("Calculate 2 + 3"))
    require(sentinel["status"] == "completed", "Worker did not settle the cancelled task")
    state = services.state(run_id)
    require(state["status"] == "cancelled" and step(state, "read")["result"] is None,
            "Late response overwrote cancellation")
    require(step(state, "publish")["attempts"] == 0 and step(state, "publish")["result"] is None,
            "Dependent publication executed after cancellation")
    events = services.events(run_id)
    require(events[-1]["type"] == "cancelled", "Cancelled run received a later durable completion")
    verify_mcp(services, run_id, "cancelled")
    checks.append({"id": EXPECTED[1], "passed": True, "dependent_publication_attempts": 0,
                   "late_result_committed": False, "http_and_mcp_agree": True})
    resumed = services.client.get(url, headers={**headers, "Last-Event-ID": str(cursor)})
    require(resumed.status_code == 200, "SSE reconnect failed")
    frames = list(sse_events(resumed.text.splitlines()))
    observed = [json.loads(event["data"]) for event in frames if event.get("event") == "workflow"]
    expected = [event for event in events if event["id"] > cursor]
    require(observed == expected and bool(observed), "Resumed SSE disagrees with durable REST events")
    ids = [event["id"] for event in observed]
    require(ids == sorted(set(ids)) and min(ids) > cursor, "SSE replay contains old or duplicate event IDs")
    require(frames[-1]["event"] == "done" and json.loads(frames[-1]["data"])["status"] == "cancelled",
            "SSE terminal state disagrees with REST")
    checks.append({"id": EXPECTED[2], "passed": True, "cursor": cursor, "resumed_events": len(ids),
                   "duplicates": 0, "persisted_events_match": True})

    question = "What is the session timeout after a transient failure?"
    fault = services.proxy.fault(question, failures=1)
    run_id = services.submit(question, steps=source_steps())
    state = services.finish(run_id)
    require(state["status"] == "completed" and step(state, "read")["attempts"] == 2,
            "Transient HTTP failure did not recover within the retry cap")
    require(fault.calls == 2 and fault.forwarded == 1, "Unexpected transient retry execution")
    require(sum(event["type"] == "retry_scheduled" for event in services.events(run_id)) == 1,
            "Retry scheduling was not durable")
    checks.append({"id": EXPECTED[3], "passed": True, "http_attempts": 2, "actual_rag_requests": 1})

    question = "What is the session timeout after repeated failure?"
    fault = services.proxy.fault(question, failures=100)
    run_id = services.submit(question, steps=source_steps(publish=True))
    state = services.finish(run_id)
    require(state["status"] == "failed" and step(state, "read")["attempts"] == 2 and fault.calls == 2,
            "HTTP failures exceeded the configured retry cap")
    require(step(state, "publish")["attempts"] == 0, "Failed dependency allowed publication")
    checks.append({"id": EXPECTED[4], "passed": True, "http_attempts": 2, "dependent_publication_attempts": 0})

    question = "What is the session timeout beyond the execution deadline?"
    fault = services.proxy.fault(question, hold_requests=1)
    run_id = services.submit(question, steps=source_steps(publish=True))
    eventually(lambda: fault.forwarded == 1, description="actual RAG response held past deadline")
    state = services.finish(run_id)
    require(state["status"] == "failed" and step(state, "read")["result"]["outcome"] == "deadline",
            "In-flight HTTP work did not obey the persisted step deadline")
    require(step(state, "read")["attempts"] == 1 and step(state, "publish")["attempts"] == 0,
            "Deadline stop was retried or published")
    fault.release.set()
    eventually(lambda: fault.finished == 1, description="deadline request cleanup")
    verify_mcp(services, run_id, "failed")
    checks.append({"id": EXPECTED[5], "passed": True, "attempts": 1, "dependent_publication_attempts": 0})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    services = Services(postgres=os.getenv("AGENT_E2E_POSTGRES_URL"))
    report = {"schema_version": "agent-resilience-e2e-v1", "status": "failed", "expected_checks": EXPECTED,
              "checks": [], "backend": "postgresql" if services.postgres else "sqlite",
              "real_services": ["rag-http", "agent-http", "independent-worker", "fixed-localhost-fault-proxy", "stdio-mcp"],
              "database_state_modified_by_test": False, "live_paid_provider": False}
    try:
        with services:
            run(services, report["checks"])
        require([check["id"] for check in report["checks"]] == EXPECTED, "An expected resilience check did not execute")
        report["status"] = "passed"
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        report["diagnostics"] = services.diagnostics
        raise
    finally:
        report["cleanup_completed"] = services.cleaned
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_report(args.report, report)
        print(json.dumps({"status": report["status"], "backend": report["backend"],
                          "completed_checks": len(report["checks"]), "expected_checks": len(EXPECTED),
                          "report": args.report}, indent=2))


if __name__ == "__main__":
    main()
