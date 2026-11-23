"""Elapsed execution cannot publish progress after a blocking write-lock wait."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agentic.features import Features
from agentic.schemas import Step, WorkflowIn
from agentic.store import LeaseLost, Store
from agentic.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def execution(tmp_path, monkeypatch):
    clock = {"now": time.time()}
    monkeypatch.setattr("agentic.store.time", SimpleNamespace(time=lambda: clock["now"]))
    store = Store(str(tmp_path / "deadline.db"), lease_seconds=60, step_timeout_seconds=10)
    run_id = store.submit("tenant", WorkflowIn(goal="Calculate six times seven", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "6*7"}),
    ]))["id"]
    return store, store.claim(), clock, run_id


def assert_deadline(store, run_id):
    run = store.get("tenant", run_id)
    assert run["status"] == "failed"
    assert len(run["steps"]) == 1
    step = run["steps"][0]
    assert step["error"] == "deadline"
    assert step["result"]["outcome"] == "deadline"
    assert step["result"]["stop_reason"] == "step_timeout"
    assert step["checkpoint"] == {}
    failures = [event for event in store.events("tenant", run_id) if event["type"] == "failed"]
    assert len(failures) == 1 and failures[0]["payload"]["outcome"] == "deadline"
    return step


def test_sqlite_completion_rechecks_clock_after_waiting_for_write_lock(execution):
    store, job, clock, run_id = execution
    entered = threading.Event()

    def complete():
        entered.set()
        return store.complete(job, {"value": 42})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with sqlite3.connect(store.path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            pending = executor.submit(complete)
            assert entered.wait(2)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            clock["now"] = job["deadline_at"] + 1
        assert pending.result(timeout=2) is False
    assert_deadline(store, run_id)


@pytest.mark.parametrize("operation", ["complete", "fail", "heartbeat", "checkpoint", "plan", "artifact"])
def test_late_mutations_commit_deadline_without_progress_or_losing_usage(execution, operation):
    store, job, clock, run_id = execution
    reservation = store.reserve_usage("tenant", run_id, "fixture", "planning", 100, 20)
    clock["now"] = job["deadline_at"] + 1
    if operation == "plan":
        with pytest.raises(LeaseLost):
            store.expand_plan(job, [Step(id="late", tool="math.calculate", args={"expression": "1+1"})])
    else:
        operations = {
            "complete": lambda: store.complete(job, {"value": 42}),
            "fail": lambda: store.fail(job, "late failure"),
            "heartbeat": lambda: store.heartbeat(job),
            "checkpoint": lambda: store.checkpoint(job, {"late": True}, event="late_progress"),
            "artifact": lambda: store.register_artifact(job, {"artifact_id": "late-artifact"}),
        }
        assert operations[operation]() is False
    assert_deadline(store, run_id)
    assert not store.heartbeat(job) and not store.complete(job, {"value": 42})
    assert_deadline(store, run_id)
    assert store.artifact_namespaces("tenant", "late-artifact") == []
    store.settle_usage(reservation, input_tokens=7, output_tokens=3, cost_microusd=10)
    record, = store.usage("tenant", run_id)["calls"]
    assert record["status"] == "reported" and record["charged_tokens"] == 10


def test_cancellation_and_stale_lease_take_precedence_over_deadline(execution):
    store, job, clock, run_id = execution
    clock["now"] = job["deadline_at"] + 1
    assert not store.complete({**job, "lease_token": "stale"}, {"value": 42})
    assert store.get("tenant", run_id)["status"] == "running"
    assert store.cancel("tenant", run_id)
    assert not store.checkpoint(job, {"late": True})
    assert store.get("tenant", run_id)["status"] == "cancelled"
    assert not any(event["type"] == "failed" for event in store.events("tenant", run_id))


async def test_worker_rechecks_local_budget_after_non_yielding_handler(execution, monkeypatch):
    store, job, _, run_id = execution
    elapsed = {"now": 0.0}
    monkeypatch.setattr("agentic.worker.time", SimpleNamespace(time=time.time, monotonic=lambda: elapsed["now"]))

    async def execute(job, inputs):
        # A synchronous operation can run past the timer before it yields once.
        elapsed["now"] = 1.0
        return {"value": 42}

    tools = SimpleNamespace(config=SimpleNamespace(step_timeout_seconds=0.1, heartbeat_seconds=1,
                                                   features=Features()), execute=execute)
    await Worker(store, tools).process(job)
    # The durable deadline is still in the future; this proves the worker's
    # independent local timeout, rather than merely Store's expiry fence.
    assert time.time() < job["deadline_at"]
    assert_deadline(store, run_id)


