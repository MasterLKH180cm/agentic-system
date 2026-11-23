"""Independent workers with renewed leases, deadlines and cancellation fencing."""
import asyncio
from contextlib import suppress
import logging
import os
import time

from agentic.config import Settings
from agentic.async_io import OffloadBusy, renew_lease, run_sync
from agentic.features import FeatureDisabled
from agentic.model import ModelGateway
from agentic.store import BudgetExceeded, LeaseLost, Store, deadline_result
from agentic.telemetry import get_telemetry, span
from agentic.tools import Tools


LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, store: Store, tools: Tools, max_parallel: int = 4):
        self.store, self.tools, self.max_parallel = store, tools, max_parallel
        self.timeout = tools.config.step_timeout_seconds
        self.heartbeat_interval = min(tools.config.heartbeat_seconds, store.lease_seconds / 4)

    async def _watch(self, job: dict, task: asyncio.Task) -> None:
        while not task.done():
            await asyncio.sleep(self.heartbeat_interval)
            if not task.done():
                try:
                    renewed = await renew_lease(self.store.heartbeat, job)
                except Exception:
                    LOG.warning("lease renewal unavailable run=%s step=%s", job["run_id"], job["id"])
                    renewed = False
                if not renewed:
                    task.cancel()
                    return

    async def process(self, job: dict) -> None:
        local_deadline = time.monotonic() + self.timeout
        persisted_deadline = job.get("deadline_at", time.time() + self.timeout)
        # An offloaded mutation can outlive cancellation while waiting for its
        # database lock. Pass the stricter local deadline into its durable fence.
        job = {**job, "worker_deadline_at": min(persisted_deadline, time.time() + self.timeout)}

        def remaining():
            return min(local_deadline - time.monotonic(), persisted_deadline - time.time())

        async def execute_and_finalize():
            try:
                async with asyncio.timeout(max(0, remaining())):
                    inputs = await run_sync(self.store.input_results, job["run_id"], job["depends_on"])
                    snapshot = job.get("specification", {}).get("feature_flags")
                    with span("workflow.step", {"run.id": job["run_id"], "step.id": job["id"],
                                                "tool.name": job["tool"]},
                              allowed=self.tools.config.features.enabled("telemetry", snapshot)):
                        result = await self.tools.execute(job, inputs)
                    # A non-yielding handler cannot evade the elapsed deadline.
                    if remaining() <= 0:
                        raise TimeoutError("Execution deadline elapsed")
                    await run_sync(self.store.complete, job, result)
            except LeaseLost:
                pass
            except BudgetExceeded:
                await run_sync(self.store.complete, job, {
                    "outcome": "budget_exhausted", "stop_reason": "model_budget",
                    "answer": "Execution stopped at the configured model budget.", "citations": [],
                })
            except TimeoutError:
                await run_sync(self.store.complete, job, deadline_result())
            except FeatureDisabled as exc:
                await run_sync(self.store.complete, job, {"outcome": "policy_denied", "stop_reason": "feature_disabled",
                                                         "feature": exc.feature, "answer": str(exc), "citations": []})
            except PermissionError:
                await run_sync(self.store.complete, job, {
                    "outcome": "policy_denied", "stop_reason": "execution_policy",
                    "answer": "Execution was denied by the workflow policy.", "citations": [],
                })
            except OffloadBusy:
                # No unbounded retry queue. The durable lease/attempt policy
                # recovers this unfinished execution when capacity returns.
                LOG.warning("persistence capacity occupied run=%s step=%s", job["run_id"], job["id"])
            except Exception as exc:
                # Exceptions may carry provider bodies or secrets. Persist only type.
                error = type(exc).__name__ + ": tool execution failed"
                LOG.warning("workflow step failed run=%s step=%s type=%s", job["run_id"], job["id"], type(exc).__name__)
                await run_sync(self.store.fail, job, error)

        # Renewal includes durable completion/failure, not only tool execution.
        task = asyncio.create_task(execute_and_finalize())
        monitor = asyncio.create_task(self._watch(job, task))
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except OffloadBusy:
            LOG.warning("persistence finalization capacity occupied run=%s step=%s", job["run_id"], job["id"])
        finally:
            if not task.done():
                task.cancel()
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def once(self) -> int:
        jobs = []
        for _ in range(self.max_parallel):
            job = await run_sync(self.store.claim)
            if job is None:
                break
            jobs.append(job)
        if jobs:
            await asyncio.gather(*(self.process(job) for job in jobs))
        return len(jobs)

    async def serve(self) -> None:
        running: set[asyncio.Task] = set()
        try:
            while True:
                while len(running) < self.max_parallel:
                    try:
                        job = await run_sync(self.store.claim)
                    except OffloadBusy:
                        break
                    if job is None:
                        break
                    running.add(asyncio.create_task(self.process(job)))
                if running:
                    done, running = await asyncio.wait(running, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                else:
                    await asyncio.sleep(0.2)
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    get_telemetry(settings.features.enabled("telemetry"))
    store = Store(settings.db_path, lease_seconds=settings.lease_seconds, max_attempts=settings.max_attempts,
                  step_timeout_seconds=settings.step_timeout_seconds)
    tools = Tools(settings, ModelGateway(settings, store), store)
    asyncio.run(Worker(store, tools, settings.max_parallel).serve())


if __name__ == "__main__":
    main()
