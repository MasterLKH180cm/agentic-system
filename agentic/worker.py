"""Independent workers with renewed leases, deadlines and cancellation fencing."""
import asyncio
from contextlib import suppress
import logging
import os
import time

from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.store import BudgetExceeded, LeaseLost, Store
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
                    renewed = await asyncio.to_thread(self.store.heartbeat, job)
                except Exception:
                    LOG.warning("lease renewal unavailable run=%s step=%s", job["run_id"], job["id"])
                    renewed = False
                if not renewed:
                    task.cancel()
                    return

    async def process(self, job: dict) -> None:
        async def execute():
            inputs = self.store.input_results(job["run_id"], job["depends_on"])
            with span("workflow.step", {"run.id": job["run_id"], "step.id": job["id"], "tool.name": job["tool"]}):
                remaining = min(self.timeout, job.get("deadline_at", time.time() + self.timeout) - time.time())
                return await asyncio.wait_for(self.tools.execute(job, inputs), timeout=max(0, remaining))

        task = asyncio.create_task(execute())
        monitor = asyncio.create_task(self._watch(job, task))
        try:
            result = await task
            self.store.complete(job, result)
        except asyncio.CancelledError:
            # A revoked/cancelled run cancels this child task without failing it.
            # Cancellation of the worker itself still propagates for shutdown.
            if asyncio.current_task().cancelling():
                raise
        except LeaseLost:
            pass
        except BudgetExceeded:
            self.store.complete(job, {"outcome": "budget_exhausted", "stop_reason": "model_budget",
                                      "answer": "Execution stopped at the configured model budget.", "citations": []})
        except TimeoutError:
            self.store.complete(job, {"outcome": "deadline", "stop_reason": "step_timeout",
                                      "answer": "Execution stopped at the configured deadline.", "citations": []})
        except PermissionError:
            self.store.complete(job, {"outcome": "policy_denied", "stop_reason": "execution_policy",
                                      "answer": "Execution was denied by the workflow policy.", "citations": []})
        except Exception as exc:
            # Exceptions may carry provider bodies or secrets. Persist only type.
            error = type(exc).__name__ + ": tool execution failed"
            LOG.warning("workflow step failed run=%s step=%s type=%s", job["run_id"], job["id"], type(exc).__name__)
            self.store.fail(job, error)
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
            job = self.store.claim()
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
                    job = await asyncio.to_thread(self.store.claim)
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
    get_telemetry()
    store = Store(settings.db_path, lease_seconds=settings.lease_seconds, max_attempts=settings.max_attempts,
                  step_timeout_seconds=settings.step_timeout_seconds)
    tools = Tools(settings, ModelGateway(settings, store), store)
    asyncio.run(Worker(store, tools, settings.max_parallel).serve())


if __name__ == "__main__":
    main()
