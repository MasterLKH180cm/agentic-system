"""Independent worker. DAG nodes with satisfied dependencies run concurrently.

Step execution is at-least-once with bounded retries and fencing on completion.
Tool effects must be idempotent; no dangerous external mutating tools are enabled.
"""
import asyncio
import logging
import os

from agentic.config import Settings
from agentic.model import ModelGateway
from agentic.store import Store
from agentic.tools import Tools


LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, store: Store, tools: Tools, max_parallel: int = 4):
        self.store = store
        self.tools = tools
        self.max_parallel = max_parallel

    async def process(self, job: dict) -> None:
        try:
            inputs = self.store.input_results(job["run_id"], job["depends_on"])
            result = await self.tools.execute(job, inputs)
            self.store.complete(job, result)
        except Exception as exc:
            # Never include secrets or document contents in the error record.
            error = type(exc).__name__ + ": tool execution failed"
            LOG.warning("workflow step failed run=%s step=%s type=%s",
                        job["run_id"], job["id"], type(exc).__name__)
            self.store.fail(job, error)

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
        while True:
            processed = await self.once()
            if not processed:
                await asyncio.sleep(0.5)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    store = Store(settings.db_path)
    tools = Tools(settings, ModelGateway(settings))
    asyncio.run(Worker(store, tools, settings.max_parallel).serve())


if __name__ == "__main__":
    main()
