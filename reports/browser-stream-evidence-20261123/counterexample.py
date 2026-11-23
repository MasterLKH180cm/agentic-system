"""Scratch-only real-PG counterexample to a cross-workflow FIFO test assumption.

Only the real public API and Worker execute state transitions. A separate SQL
transaction temporarily locks one synthetic run with SELECT FOR UPDATE. No
production monkeypatch, database UPDATE/DELETE, or real model is used.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time


ROOT = Path("/workspace/scratch/7113f13a25c9/agentic-system")
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def utc():
    return datetime.now(timezone.utc).isoformat()


def once():
    from agentic.config import Settings
    from agentic.model import ModelGateway
    from agentic.store import Store
    from agentic.tools import Tools
    from agentic.worker import Worker

    settings = Settings.from_env()
    assert settings.max_parallel == 1
    store = Store(settings.db_path, lease_seconds=settings.lease_seconds,
                  max_attempts=settings.max_attempts,
                  step_timeout_seconds=settings.step_timeout_seconds)
    tools = Tools(settings, ModelGateway(settings, store), store)
    count = asyncio.run(Worker(store, tools, settings.max_parallel).once())
    assert count == 1
    print(json.dumps({"real_worker_once_claimed": count}))


def reduced_state(services, run_id):
    state = services.state(run_id)
    return {"id": state["id"], "status": state["status"],
            "steps": [{"id": s["id"], "tool": s["tool"],
                       "status": s["status"], "attempts": s["attempts"],
                       "outcome": (s.get("result") or {}).get("outcome")}
                      for s in state["steps"]]}


def main(output):
    import psycopg
    from e2e_support import Services, eventually, require

    report = {"schema_version": "browser-held-gate-counterexample-v1", "status": "running",
              "started_at": utc(), "synthetic_only": True, "model_provider": "offline",
              "injected_faults": ["temporary SELECT FOR UPDATE row lock",
                                  "hold second real RAG response at local proxy"],
              "direct_database_data_mutations": False,
              "production_monkeypatches": False,
              "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in ["agentic/store.py", "agentic/postgres.py", "agentic/worker.py",
                                             "agentic/api.py", "agentic/interfaces.py", "tests/e2e_support.py"]},
              "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "checkpoints": []}
    started = time.monotonic()
    services = Services(step_timeout=45, postgres=os.environ["AGENT_E2E_POSTGRES_URL"],
                        rag_postgres=os.environ["RAGS_E2E_POSTGRES_URL"])
    second_fault = None
    first = second = None
    failure = None
    try:
        with services:
            report["storage"] = {"agent": services.agent_storage, "rag": services.rag_storage}
            report["settings"] = {key: services.env[key] for key in (
                "AGENT_MAX_PARALLEL", "AGENT_LEASE_SECONDS", "AGENT_HEARTBEAT_SECONDS",
                "AGENT_STEP_TIMEOUT_SECONDS", "AGENT_RAG_TIMEOUT_SECONDS")}
            # Stop the normal empty worker before any workflow is submitted.
            os.killpg(services.worker.pid, signal.SIGTERM)
            services.worker.wait(timeout=4)
            first = services.submit("What is the session timeout for the first browser stream?")
            worker_once = services.start("worker-once", [sys.executable, str(Path(__file__)), "--once"])
            require(worker_once.wait(timeout=15) == 0, "Real Worker.once child failed")
            first_state = reduced_state(services, first)
            require([s["status"] for s in first_state["steps"]] == ["completed", "pending"],
                    "First workflow must have durable retrieval completed and summary pending")
            report["checkpoints"].append({"label": "first_retrieval_committed", "at": utc(),
                                          "first": first_state})

            second_goal = "What is the session timeout for the second browser stream?"
            second_fault = services.proxy.fault(second_goal, hold_requests=1)
            second = services.submit(second_goal)
            # Ordinary read/write transaction is required by PG for FOR UPDATE;
            # the only statement concerning application data is this SELECT.
            with psycopg.connect(services.env["AGENT_DATABASE_URL"]) as lock:
                lock.execute("SET LOCAL lock_timeout = '3s'")
                lock.execute("SET LOCAL statement_timeout = '3s'")
                found = lock.execute("SELECT id FROM runs WHERE id=%s AND tenant=%s FOR UPDATE",
                                     (first, services.tenant)).fetchone()
                require(found == (first,), "Failed to lock the owned synthetic first run")
                services.start_worker("worker-adverse-schedule")
                eventually(lambda: second_fault.forwarded == 1, timeout=10,
                           description="second real RAG request while first row is locked")
                report["checkpoints"].append({"label": "skip_locked_selected_second", "at": utc(),
                                              "first": reduced_state(services, first),
                                              "second": reduced_state(services, second),
                                              "second_gate_forwarded": second_fault.forwarded,
                                              "second_gate_released": second_fault.release.is_set()})
            # The DB lock has ended; only the fixture's un-released RAG response
            # occupies the single normal worker now.
            require(not second_fault.release.is_set(), "Second RAG gate released unexpectedly")
            first_wait_started = time.monotonic()
            timed_out = False
            try:
                services.finish(first, timeout=1)
            except AssertionError as error:
                require(str(error) == "Deadline waiting for terminal workflow " + first,
                        "First terminal wait failed for an unrelated reason")
                timed_out = True
            require(timed_out, "First workflow unexpectedly became terminal while second gate held")
            first_state, second_state = reduced_state(services, first), reduced_state(services, second)
            require([s["status"] for s in first_state["steps"]] == ["completed", "pending"],
                    "First summary must remain pending after the database lock is released")
            require([s["status"] for s in second_state["steps"]] == ["leased", "pending"],
                    "Second retrieval must occupy the only worker while the response is held")
            report["checkpoints"].append({"label": "terminal_wait_cannot_progress_with_second_gate_held",
                                          "at": utc(), "first": first_state, "second": second_state,
                                          "bounded_wait_seconds": time.monotonic() - first_wait_started,
                                          "first_terminal_wait_timed_out": timed_out,
                                          "second_gate_forwarded": second_fault.forwarded,
                                          "second_gate_finished": second_fault.finished})
            second_fault.release.set()
            states = [services.finish(run_id) for run_id in (first, second)]
            for state in states:
                require(state["status"] == "completed", "Workflow failed after all gates released")
                require(all(s["status"] == "completed" and s["attempts"] == 1 for s in state["steps"]),
                        "Unexpected failed/recovered/retried step")
                result = state["steps"][-1]["result"]
                require(result["outcome"] == "success" and "30 minutes" in result["answer"]
                        and bool(result["citations"]), "Final summary lacks real grounded fixture evidence")
            report["checkpoints"].append({"label": "both_complete_after_second_gate_release", "at": utc(),
                                          "first": reduced_state(services, first),
                                          "second": reduced_state(services, second),
                                          "grounded_answer_checks": 2,
                                          "second_gate_finished": second_fault.finished})
            report["events"] = {label: [{"id": event["id"], "run_id": event["run_id"],
                                         "step_id": event["step_id"], "type": event["type"]}
                                        for event in services.events(run_id)]
                                for label, run_id in (("first", first), ("second", second))}
            report["status"] = "passed"
    except Exception as error:
        failure = error
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
    finally:
        if second_fault is not None:
            second_fault.release.set()
        report["cleanup_completed"] = services.cleaned
        report["elapsed_seconds"] = time.monotonic() - started
        report["finished_at"] = utc()
        with Path(output).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"status": report["status"], "cleanup_completed": services.cleaned,
                          "elapsed_seconds": report["elapsed_seconds"], "report": str(output)}))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.once:
        once()
    elif args.output:
        main(args.output)
    else:
        parser.error("--output is required unless --once is selected")
