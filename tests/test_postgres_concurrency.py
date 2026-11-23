"""Actual PostgreSQL concurrency and pooled transaction invariants."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
import uuid

import pytest

from agentic.pgpool import close_pools
from agentic.schemas import Step, WorkflowIn
from agentic.store import BudgetExceeded, Store

pytestmark = [pytest.mark.integration, pytest.mark.external, pytest.mark.postgres]


@pytest.fixture
def database():
    import psycopg
    from psycopg import sql

    endpoint = os.getenv("AGENT_TEST_POSTGRES_URL")
    if not endpoint:
        pytest.fail("AGENT_TEST_POSTGRES_URL is required for selected concurrency tests")
    schema = "concurrency_test_" + uuid.uuid4().hex
    parsed = urlsplit(endpoint)
    # Use an owned schema exclusively. Never touch a caller's application tables.
    dsn = urlunsplit(parsed._replace(query=urlencode({"options": f"-c search_path={schema}"}, quote_via=quote)))
    with psycopg.connect(endpoint, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield dsn
        finally:
            close_pools(dsn)
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def submit(store, *, steps=None, specification=None):
    return store.submit("tenant", WorkflowIn(goal="Concurrent calculation", steps=steps or [
        Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
    ]), specification=specification)["id"]


def test_pool_reuses_connection_and_direct_mode_is_explicit(database, monkeypatch):
    store = Store(database)
    with store.connect(read_only=True) as connection:
        first = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT pg_backend_pid()").fetchone()[0] == first
    monkeypatch.setenv("AGENT_PG_POOL_SIZE", "0")
    with store.connect(read_only=True) as connection:
        direct = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT pg_backend_pid()").fetchone()[0] != direct


def test_reads_ignore_control_lock_and_use_one_read_only_snapshot(database):
    import psycopg

    store = Store(database)
    run = submit(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.connect():  # Hold the old global advisory write lock.
            assert executor.submit(store.get, "tenant", run).result(timeout=2)["status"] == "queued"
        with store.connect(read_only=True) as snapshot:
            before = snapshot.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            assert executor.submit(store.cancel, "tenant", run).result(timeout=2)
            assert snapshot.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == before
    assert store.get("tenant", run)["status"] == "cancelled"
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), store.connect(read_only=True) as snapshot:
        snapshot.execute("DELETE FROM runs WHERE id=?", (run,))


def test_claim_skips_locked_run_and_later_claims_it(database):
    store = Store(database)
    first, second = submit(store), submit(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._run_connection(first):
            job = executor.submit(store.claim).result(timeout=2)
            assert job["run_id"] == second
    assert store.claim()["run_id"] == first


def test_legacy_exclusive_write_lock_blocks_new_mutation_but_not_snapshot_reads(database):
    store = Store(database)
    run = submit(store)
    entered = threading.Event()

    def cancel():
        entered.set()
        return store.cancel("tenant", run)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with store.connect():  # Exact advisory key/mode used by the v0.4 runtime.
            mutation = executor.submit(cancel)
            assert entered.wait(timeout=2)
            with pytest.raises(FutureTimeout):
                mutation.result(timeout=0.1)
            assert executor.submit(store.get, "tenant", run).result(timeout=2)["status"] == "queued"
        assert mutation.result(timeout=2)


def test_shared_compatibility_lock_allows_unrelated_workflow_mutation(database):
    store = Store(database)
    first, second = submit(store), submit(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._run_connection(first, tenant="tenant"):
            assert executor.submit(store.cancel, "tenant", second).result(timeout=2)
    assert store.get("tenant", first)["status"] == "queued"


def test_foreign_tenant_requests_do_not_wait_on_or_mutate_a_known_workflow(database):
    store = Store(database)
    run = submit(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._run_connection(run, tenant="tenant"):
            assert not executor.submit(store.cancel, "foreign", run).result(timeout=2)
            assert not executor.submit(store.approve, "foreign", run, "calculate", True, "").result(timeout=2)
    job = store.claim()
    forged = {**job, "tenant": "foreign"}
    assert not store.complete(forged, {"value": 0})
    assert not store.fail(forged, "foreign failure")
    assert store.complete(job, {"value": 42})


def test_concurrent_claims_and_completions_are_unique_and_fenced(database):
    store = Store(database)
    runs = [submit(store, steps=[Step(id=f"step{number}", tool="math.calculate", args={"expression": "6 * 7"})
                                for number in range(3)]) for _ in range(8)]
    barrier = threading.Barrier(8)

    def worker(_):
        barrier.wait(timeout=5)
        jobs = []
        while job := store.claim():
            jobs.append(job)
            assert store.complete(job, {"value": 42})
        return jobs

    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = [job for result in executor.map(worker, range(8)) for job in result]
    assert len(jobs) == len({(job["run_id"], job["id"]) for job in jobs}) == 24
    for run in runs:
        assert store.get("tenant", run)["status"] == "completed"
        events = store.events("tenant", run)
        assert sum(event["type"] == "started" for event in events) == 3
        assert sum(event["type"] == "completed" for event in events) == 3
    assert not store.complete(jobs[0], {"value": "stale"})


def test_budget_reservations_and_repeated_settlements_remain_atomic(database):
    store = Store(database)
    run = submit(store, specification={"max_model_tokens": 100})

    def reserve(_):
        try:
            return store.reserve_usage("tenant", run, "test", "synthesis", 40, 10)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        reservations = [result for result in executor.map(reserve, range(8)) if result]
    assert len(reservations) == 2
    assert store.usage("tenant", run)["charged_tokens"] == 80
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: store.settle_usage(reservations[0], input_tokens=3, output_tokens=2,
                                                      cost_microusd=4), range(8)))
    assert store.usage("tenant", run)["charged_tokens"] == 45
    assert sum(event["type"] == "model_usage" for event in store.events("tenant", run)) == 1


def test_dependency_approval_recovery_and_cancellation_contract(database):
    store = Store(database, lease_seconds=30, max_attempts=2, step_timeout_seconds=10)
    run = submit(store, steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
        Step(id="publish", tool="artifact.publish", args={},
             depends_on=["calculate"], approval_required=True),
    ])
    first = store.claim()
    assert first["id"] == "calculate" and store.claim() is None
    with store._run_connection(run) as connection:
        connection.execute("UPDATE steps SET lease_until=? WHERE run_id=? AND id=?",
                           (time.time() - 1, run, "calculate"))
    recovered = store.claim()
    assert recovered["lease_token"] != first["lease_token"]
    assert recovered["deadline_at"] == first["deadline_at"] and recovered["attempts"] == 1
    assert not store.complete(first, {"value": 42})
    assert store.complete(recovered, {"value": 42})
    assert store.claim() is None
    assert store.get("tenant", run)["status"] == "waiting_approval"
    assert store.approve("tenant", run, "publish", True, "Reviewed")
    effect = store.claim()
    assert effect["id"] == "publish"
    assert store.cancel("tenant", run)
    assert not store.heartbeat(effect) and not store.checkpoint(effect, {"late": True})
    assert not store.complete(effect, {"value": "late"})


def test_expired_attempt_limit_and_persisted_deadline_do_not_reset(database):
    store = Store(database, max_attempts=1)
    run = submit(store)
    original = store.claim()
    with store._run_connection(run) as connection:
        connection.execute("UPDATE steps SET lease_until=? WHERE run_id=?", (time.time() - 1, run))
    assert store.claim() is None
    assert store.get("tenant", run)["steps"][0]["attempts"] == 1
    assert store.get("tenant", run)["status"] == "failed"
    assert not store.complete(original, {"value": 42})
    deadline_run = submit(store)
    with store._run_connection(deadline_run) as connection:
        connection.execute("UPDATE steps SET deadline_at=? WHERE run_id=?", (time.time() - 1, deadline_run))
    assert store.claim() is None
    assert store.get("tenant", deadline_run)["steps"][0]["result"]["outcome"] == "deadline"


def test_deadline_is_rechecked_after_a_postgres_row_lock_wait(database, monkeypatch):
    clock = {"now": time.time()}
    monkeypatch.setattr("agentic.store.time", SimpleNamespace(time=lambda: clock["now"]))
    store = Store(database, lease_seconds=60, step_timeout_seconds=10)
    run = submit(store)
    job = store.claim()
    entered = threading.Event()

    def complete():
        entered.set()
        return store.complete(job, {"value": 42})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._run_connection(run, tenant="tenant"):
            pending = executor.submit(complete)
            assert entered.wait(2)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            clock["now"] = job["deadline_at"] + 1
        assert pending.result(timeout=2) is False
    result = store.get("tenant", run)
    assert result["status"] == "failed"
    assert result["steps"][0]["result"]["outcome"] == "deadline"
    assert result["steps"][0]["result"]["stop_reason"] == "step_timeout"
