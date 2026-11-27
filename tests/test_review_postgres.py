"""Actual PostgreSQL review regressions: locks, renewal and accounting scope."""
import asyncio
from dataclasses import replace
import threading
import time

import pytest

from agentic.async_io import run_sync
from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.schemas import Step, WorkflowIn
from agentic.store import Store
from agentic.tools import Tools
from agentic.worker import Worker
from test_postgres_concurrency import database as database
from test_model_failures import provider as provider, settled_usage

pytestmark = [pytest.mark.integration, pytest.mark.external, pytest.mark.postgres]


async def test_locked_run_does_not_block_unrelated_completion_or_lease_renewal(database, tmp_path, monkeypatch):
    import psycopg

    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    config = Settings(db_path=database, keys={"fixture": "tenant"}, rag_url="http://127.0.0.1:1",
                      rag_keys={"tenant": "fixture-rag"}, provider="offline", openai_key="",
                      openai_base_url="https://example.invalid", openai_model="fixture",
                      anthropic_key="", anthropic_model="fixture", max_parallel=3,
                      lease_seconds=0.6, heartbeat_seconds=0.1, step_timeout_seconds=5)
    store = Store(database, lease_seconds=config.lease_seconds, step_timeout_seconds=config.step_timeout_seconds)
    ids = [store.submit("tenant", WorkflowIn(goal=f"Calculate {number} * 7", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": f"{number} * 7"}),
    ]))["id"] for number in (6, 7, 8)]
    jobs = [store.claim() for _ in ids]
    assert [job["run_id"] for job in jobs] == ids
    tools = Tools(config, ModelGateway(config, store), store)
    execute = tools.execute

    async def slow_third(job, inputs):
        if job["run_id"] == ids[2]:
            await asyncio.sleep(0.9)  # Must renew beyond the original 0.6s lease.
        return await execute(job, inputs)

    monkeypatch.setattr(tools, "execute", slow_third)
    entered, release = threading.Event(), threading.Event()

    def hold_first_run():
        with psycopg.connect(database) as connection:
            connection.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (ids[0],))
            entered.set()
            assert release.wait(4)

    blocker = threading.Thread(target=hold_first_run)
    blocker.start()
    assert await asyncio.to_thread(entered.wait, 2)
    worker = Worker(store, tools, max_parallel=3)
    tasks = [asyncio.create_task(worker.process(job)) for job in jobs]
    try:
        await asyncio.wait_for(tasks[1], 0.5)
        await asyncio.wait_for(tasks[2], 1.8)
        for run_id, answer in ((ids[1], 49), (ids[2], 56)):
            run = await run_sync(store.get, "tenant", run_id)
            assert run["status"] == "completed"
            assert run["steps"][0]["result"]["value"] == answer
            assert run["steps"][0]["attempts"] == 1
        assert not tasks[0].done()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(blocker.join, 2)
    # Its own lock blocked its lease renewal, so the first run is safely fenced.
    assert store.get("tenant", ids[0])["steps"][0]["result"] is None
    # Cancellation ends the asyncio waiter before admitted synchronous work
    # releases its transaction. SKIP LOCKED must be allowed to report no claim
    # while that fenced completion/heartbeat still holds the run row.
    async with asyncio.timeout(2):
        while (recovered := await run_sync(store.claim)) is None:
            await asyncio.sleep(0.01)
    assert recovered["run_id"] == ids[0] and recovered["attempts"] == 1
    assert recovered["lease_token"] != jobs[0]["lease_token"]
    assert store.get("tenant", ids[0])["steps"][0]["result"] is None
    assert store.claim() is None


def test_postgres_usage_scope_matches_sqlite_and_excludes_unbound_and_other_tenants(database):
    store = Store(database)
    for tenant, label, specification in (
        ("tenant", "allowed", {"memory_namespace": "allowed"}),
        ("tenant", "private", {"memory_namespace": "private"}),
        ("tenant", "legacy", {}), ("tenant", "null", {"memory_namespace": None}),
        ("foreign", "foreign", {"memory_namespace": "allowed"}),
    ):
        run = store.submit(tenant, WorkflowIn(goal="Calculate 6 * 7", steps=[
            Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
        ]), specification=specification)["id"]
        store.reserve_usage(tenant, run, label, "synthesis", 10, 5)
    store.reserve_usage("tenant", None, "unbound", "synthesis", 100, None)
    assert {record["route"] for record in store.usage("tenant", namespaces=["allowed", "default"])["calls"]} == {
        "allowed", "legacy",
    }
    usage = store.usage("tenant", namespaces=["allowed", "default"])
    assert usage["charged_tokens"] == 20 and usage["estimated_cost_usd"] == 10 / 1e6
    assert store.usage("tenant", namespaces=[])["calls"] == []
    assert len(store.usage("tenant")["calls"]) == 5


async def test_local_worker_timeout_still_fences_a_late_offloaded_completion(database, tmp_path, monkeypatch):
    import psycopg

    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    config = replace(Settings.from_env(), db_path=database, step_timeout_seconds=0.15)
    store = Store(database, lease_seconds=5, step_timeout_seconds=10)
    run = store.submit("local", WorkflowIn(goal="Calculate 6 * 7", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
    ]))["id"]
    job = store.claim()
    entered = threading.Event()

    def hold():
        with psycopg.connect(database) as connection:
            connection.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run,))
            entered.set()
            time.sleep(0.4)

    blocker = threading.Thread(target=hold)
    blocker.start()
    assert await asyncio.to_thread(entered.wait, 2)
    await Worker(store, Tools(config, ModelGateway(config, store), store)).process(job)
    await asyncio.to_thread(blocker.join, 2)
    state = store.get("local", run)
    assert state["status"] == "failed" and state["steps"][0]["result"]["outcome"] == "deadline"
    assert not any(event["type"] == "completed" for event in store.events("local", run))


async def test_model_deadline_does_not_wait_for_contended_postgres_accounting(database, provider):
    import psycopg

    fixture_model, _, _, state = provider
    store = Store(database)
    config = replace(fixture_model.config, db_path=database, model_timeout_seconds=0.4)
    model = ModelGateway(config, store)
    run = store.submit("tenant-a", WorkflowIn(goal="Calculate 6 * 7", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "6 * 7"}),
    ]))["id"]
    state["hold"] = True
    entered, release = threading.Event(), threading.Event()

    def hold_accounting_lock():
        with psycopg.connect(database) as connection:
            connection.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run,))
            entered.set()
            assert release.wait(3)

    task = asyncio.create_task(model.complete("System", "Question", tenant="tenant-a", run_id=run))
    blocker = threading.Thread(target=hold_accounting_lock)
    try:
        await asyncio.wait_for(state["received"].wait(), 1)
        blocker.start()
        assert await asyncio.to_thread(entered.wait, 1)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.8)
        assert task.done(), "Accounting lock must not outlive the model caller's deadline"
        assert time.monotonic() - started < 0.7
        pending = store.usage("tenant-a", run)
        assert pending["calls"][0]["status"] == "reserved"
        assert pending["charged_tokens"] == ModelGateway.input_units("System", "Question") + 32
    finally:
        release.set()
        state["release"].set()
        if blocker.ident is not None:
            await asyncio.to_thread(blocker.join, 2)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    usage = await settled_usage(store, run)
    assert usage["calls"][0]["status"] == "uncertain_timeout"
    assert len(state["calls"]) == 1
