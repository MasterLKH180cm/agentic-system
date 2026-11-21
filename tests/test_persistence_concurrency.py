"""Actual SQLite transactions, concurrent workers and durable failure contracts."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn
from agentic.store import BudgetExceeded, LeaseLost, QueueFull, Store
from agentic.tools import Tools
from agentic.worker import Worker

pytestmark = pytest.mark.integration


def workflow(key=None, expression="2 + 3"):
    return WorkflowIn(goal="Calculate a total", idempotency_key=key, steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": expression})
    ])


def concurrent(count, action):
    ready = threading.Barrier(count)

    def run(index):
        ready.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


@pytest.fixture
def stores(tmp_path):
    path = str(tmp_path / "durable.db")
    return Store(path), Store(path)


def test_sqlite_setup_failure_closes_the_acquired_connection(tmp_path, monkeypatch):
    path = tmp_path / "invalid.db"
    path.write_bytes(b"This is not a SQLite database.")
    original_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("agentic.store.sqlite3.connect", connect)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            Store(str(path))
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


def test_concurrent_idempotency_is_tenant_scoped_and_does_not_consume_capacity(stores):
    first, second = stores
    payload = workflow("same-request")
    accepted = concurrent(8, lambda index: stores[index % 2].submit("tenant-a", payload, max_queued_runs=1))
    run_id = accepted[0]["id"]
    assert {result["id"] for result in accepted} == {run_id}
    assert sum(not result["duplicate"] for result in accepted) == 1
    assert [event["type"] for event in first.events("tenant-a", run_id)] == ["created"]
    assert second.submit("tenant-a", payload, max_queued_runs=1) == {"id": run_id, "duplicate": True}
    with pytest.raises(QueueFull):
        second.submit("tenant-a", workflow("other-request"), max_queued_runs=1)
    with pytest.raises(ValueError, match="different workflow"):
        second.submit("tenant-a", workflow("same-request", "9 + 9"), max_queued_runs=1)
    foreign = second.submit("tenant-b", payload, max_queued_runs=1)
    assert foreign["id"] != run_id and not foreign["duplicate"]
    assert second.get("tenant-b", run_id) is None
    assert second.events("tenant-b", run_id) == []
    assert len(first.list("tenant-a")) == 1
    assert len(first.events("tenant-a", run_id)) == 1


def test_concurrent_admission_never_exceeds_tenant_queue_limit(stores):
    first, _ = stores

    def submit(index):
        try:
            return stores[index % 2].submit("tenant-a", workflow(f"request-{index}"), max_queued_runs=3)
        except QueueFull:
            return None

    results = concurrent(8, submit)
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 3 and results.count(None) == 5
    assert len(first.list("tenant-a")) == 3
    assert all(len(first.events("tenant-a", result["id"])) == 1 for result in accepted)
    assert first.cancel("tenant-a", accepted[0]["id"])
    replacement = first.submit("tenant-a", workflow("replacement"), max_queued_runs=3)
    assert replacement["id"] not in {result["id"] for result in accepted}
    assert first.submit("tenant-b", workflow("independent"), max_queued_runs=3)


def test_failed_storage_transaction_leaves_no_run_steps_or_events(stores):
    first, _ = stores
    # Internal compilation uses model_copy; storage still must roll back a
    # constraint violation after the run and an earlier step were inserted.
    valid = workflow("rollback")
    invalid_compilation = valid.model_copy(update={"steps": valid.steps + valid.steps})
    with pytest.raises(sqlite3.IntegrityError):
        first.submit("tenant-a", invalid_compilation)
    with first.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    accepted = first.submit("tenant-a", valid)
    assert not accepted["duplicate"]


def test_concurrent_claims_are_unique_and_wait_for_all_dependencies(stores):
    first, second = stores
    roots = [Step(id=f"root{index}", tool="math.calculate", args={"expression": str(index)})
             for index in range(4)]
    merge = Step(id="merge", tool="data.merge", depends_on=[step.id for step in roots])
    run = first.submit("tenant-a", WorkflowIn(goal="Merge independent results", steps=[merge, *roots]))
    claims = concurrent(8, lambda index: stores[index % 2].claim())
    jobs = [job for job in claims if job is not None]
    assert len(jobs) == 4 and claims.count(None) == 4
    assert {job["id"] for job in jobs} == {step.id for step in roots}
    assert len({job["lease_token"] for job in jobs}) == 4
    for index, job in enumerate(jobs):
        if index < 3:
            assert first.complete(job, {"value": index})
            assert second.claim() is None
        else:
            assert second.complete(job, {"value": index})
    dependent = first.claim()
    assert dependent["id"] == "merge"
    inputs = second.input_results(run["id"], dependent["depends_on"])
    assert set(inputs) == {step.id for step in roots}
    assert second.complete(dependent, {"outputs": inputs})
    assert first.get("tenant-a", run["id"])["status"] == "completed"
    started = [event for event in first.events("tenant-a", run["id"]) if event["type"] == "started"]
    assert len(started) == 5
    assert all(event["payload"]["attempt"] == 1 for event in started)


def test_concurrent_usage_settlement_commits_one_immutable_charge_and_event(stores):
    first, second = stores
    run_id = first.submit("tenant-a", workflow())["id"]
    for trial in range(3):
        reservation = first.reserve_usage("tenant-a", run_id, f"route-{trial}", "synthesis", 100, 1000)

        def settle(index, reservation=reservation):
            stores[index % 2].settle_usage(reservation, input_tokens=index + 1, output_tokens=2,
                                          cost_microusd=index + 10)

        concurrent(8, settle)
        row = next(row for row in second.usage("tenant-a", run_id)["calls"] if row["id"] == reservation)
        assert row["status"] == "reported"
        assert 1 <= row["input_tokens"] <= 8 and row["output_tokens"] == 2
        assert row["charged_tokens"] == row["input_tokens"] + 2
        assert row["cost_microusd"] == row["input_tokens"] + 9
        events = [event for event in first.events("tenant-a", run_id)
                  if event["type"] == "model_usage" and event["payload"]["route"] == f"route-{trial}"]
        assert len(events) == 1
        assert events[0]["payload"]["input_tokens"] == row["input_tokens"]
        second.settle_usage(reservation, input_tokens=99, output_tokens=99, cost_microusd=99)
        second.settle_usage(reservation, status="uncertain_cancelled")
        assert next(record for record in first.usage("tenant-a", run_id)["calls"]
                    if record["id"] == reservation) == row
    first.settle_usage("missing-reservation", status="uncertain_cancelled")
    assert sum(event["type"] == "model_usage" for event in first.events("tenant-a", run_id)) == 3
    assert first.usage("tenant-b", run_id)["calls"] == []


def test_reservations_reject_exhausted_foreign_and_terminal_workflows(stores):
    first, _ = stores
    run_id = first.submit("tenant-a", workflow(), specification={"max_model_tokens": 50})["id"]
    first.reserve_usage("tenant-a", run_id, "route", "synthesis", 50, None)
    with pytest.raises(BudgetExceeded):
        first.remaining_model_tokens("tenant-a", run_id, 1000)
    with pytest.raises(LeaseLost):
        first.reserve_usage("tenant-b", run_id, "route", "synthesis", 1, None)
    assert first.cancel("tenant-a", run_id)
    with pytest.raises(LeaseLost):
        first.remaining_model_tokens("tenant-a", run_id, 1000)
    with pytest.raises(LeaseLost):
        first.reserve_usage("tenant-a", run_id, "route", "synthesis", 1, None)
    assert first.remaining_model_tokens("tenant-a", None, 17) == 17
    unbound = first.reserve_usage("tenant-a", None, "route", "synthesis", 10, None)
    first.settle_usage(unbound, status="uncertain_transport_error")
    assert first.usage("tenant-a")["charged_tokens"] == 60


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ARTIFACT_BACKEND", "local")
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    config = Settings(
        db_path=str(tmp_path / "runtime.db"), keys={"test-key": "tenant-a"},
        rag_url="http://127.0.0.1:1", rag_keys={"tenant-a": "unused"}, provider="offline",
        openai_key="", openai_base_url="https://example.invalid", openai_model="fixture",
        anthropic_key="", anthropic_model="fixture", max_parallel=1,
    )
    store = Store(config.db_path, max_attempts=3)
    model = ModelGateway(config, store)
    tools = Tools(config, model, store)
    return store, tools


async def test_real_tool_failure_obeys_backoff_attempt_cap_and_dependency_blocking(runtime, monkeypatch, caplog):
    store, tools = runtime
    clock = [1000.0]
    monkeypatch.setattr("agentic.store.time.time", lambda: clock[0])
    payload = workflow(expression="1 / 0")
    payload.steps.append(Step(id="dependent", tool="data.merge", depends_on=["calculate"]))
    run_id = store.submit("tenant-a", payload)["id"]
    worker = Worker(store, tools, max_parallel=1)
    for attempt in range(1, 4):
        assert await worker.once() == 1
        run = store.get("tenant-a", run_id)
        assert run["steps"][0]["attempts"] == attempt
        assert run["steps"][0]["error"] == "ZeroDivisionError: tool execution failed"
        assert run["steps"][1]["status"] == "pending"
        assert await worker.once() == 0
        if attempt < 3:
            available_at = clock[0] + 2**attempt
            clock[0] = available_at - 0.01
            assert await worker.once() == 0
            clock[0] = available_at
    assert run["status"] == "failed"
    clock[0] += 1000
    assert store.claim() is None
    events = store.events("tenant-a", run_id)
    assert [event["payload"]["attempt"] for event in events if event["type"] == "retry_scheduled"] == [1, 2]
    assert sum(event["type"] == "failed" for event in events) == 1
    assert "1 / 0" not in caplog.text


async def test_policy_denial_is_terminal_without_retry_or_dependent_execution(runtime):
    store, tools = runtime
    run_id = store.submit("tenant-a", workflow(), specification={"allowed_tools": []})["id"]
    assert await Worker(store, tools).once() == 1
    run = store.get("tenant-a", run_id)
    assert run["status"] == "failed"
    assert run["steps"][0]["result"]["outcome"] == "policy_denied"
    assert run["steps"][0]["attempts"] == 1
    assert "retry_scheduled" not in {event["type"] for event in store.events("tenant-a", run_id)}
