"""Admission and cancellation semantics of the bounded persistence executor."""
import asyncio
from contextvars import ContextVar
import threading

import pytest

from agentic.async_io import BoundedExecutor, OffloadBusy

pytestmark = pytest.mark.unit


async def test_cancelled_running_operation_keeps_its_capacity_until_thread_finishes():
    pool = BoundedExecutor(workers=1, capacity=1, name="fixture-bounded")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(2)
        finished.set()

    task = asyncio.create_task(pool.run(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(OffloadBusy):
            await pool.run(lambda: "must not enter")
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        assert await pool.run(lambda: 42) == 42
    finally:
        release.set()
        pool.close()


async def test_bounded_queue_and_context_propagation_do_not_block_an_independent_lane():
    operations = BoundedExecutor(workers=1, capacity=2, name="fixture-operation")
    heartbeat = BoundedExecutor(workers=1, capacity=1, name="fixture-heartbeat")
    entered, release = threading.Event(), threading.Event()
    identity = ContextVar("fixture_identity", default="unset")
    identity.set("tenant-fixture")

    def blocked():
        entered.set()
        assert release.wait(2)
        return identity.get()

    first = asyncio.create_task(operations.run(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        queued = asyncio.create_task(operations.run(lambda: "queued"))
        await asyncio.sleep(0)
        with pytest.raises(OffloadBusy):
            await operations.run(lambda: "overflow")
        assert await heartbeat.run(identity.get) == "tenant-fixture"
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        # Cancelled waiters cannot fill the executor's internal queue with
        # cancelled work items whose admission slots were prematurely released.
        with pytest.raises(OffloadBusy):
            await operations.run(lambda: "cancelled queue overflow")
        release.set()
        assert await first == "tenant-fixture"
    finally:
        release.set()
        operations.close()
        heartbeat.close()
