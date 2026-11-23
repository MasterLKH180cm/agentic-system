"""Bounded process-owned offload for synchronous persistence and storage.

Admission follows the concurrent future, not its asyncio waiter: cancelling a
waiter cannot free a slot while a database thread is still running. Lease
renewal has separate capacity from ordinary application work.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import os
import threading


class OffloadBusy(RuntimeError):
    """The finite persistence admission limit is occupied."""


class BoundedExecutor:
    def __init__(self, *, workers: int, capacity: int, name: str):
        if not 1 <= workers <= capacity <= 128:
            raise ValueError("Invalid synchronous offload limits")
        self._owner = os.getpid()
        self._slots = threading.BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)

    def submit(self, function, /, *args, **kwargs):
        if os.getpid() != self._owner:
            raise RuntimeError("Synchronous executor belongs to the parent process; use spawn/exec")
        if not self._slots.acquire(blocking=False):
            raise OffloadBusy("Synchronous operation capacity is occupied")
        try:
            future = self._executor.submit(copy_context().run, partial(function, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        # Do not let waiter cancellation cancel queued concurrent futures:
        # ThreadPoolExecutor retains their queue items until a worker drains
        # them, so releasing admission on cancellation could accumulate an
        # unbounded queue of cancelled items behind one blocked operation.
        # Retrieving background errors avoids unobserved-future warnings after
        # a caller's deadline; the caller still receives errors while awaiting.
        wrapped.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        return wrapped

    async def run(self, function, /, *args, **kwargs):
        return await asyncio.shield(self.submit(function, *args, **kwargs))

    def close(self):
        """Stop an owned executor only after its callers have stopped."""
        self._executor.shutdown(wait=True, cancel_futures=True)


# AGENT_MAX_PARALLEL supports up to 16 executions. A finite extra admission
# allowance covers API requests and usage finalization without an unbounded
# ThreadPoolExecutor work queue. Heartbeats cannot queue behind ordinary work.
_operations = BoundedExecutor(workers=16, capacity=64, name="agent-io")
_heartbeats = BoundedExecutor(workers=16, capacity=16, name="agent-lease")


async def run_sync(function, /, *args, **kwargs):
    return await _operations.run(function, *args, **kwargs)


def submit_sync(function, /, *args, **kwargs):
    """Start bounded finalization whose durable charge may outlive cancellation."""
    return _operations.submit(function, *args, **kwargs)


async def renew_lease(function, /, *args, **kwargs):
    return await _heartbeats.run(function, *args, **kwargs)
