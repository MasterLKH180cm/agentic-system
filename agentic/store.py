"""Durable workflow storage with leases, fencing tokens and append-only events.

SQLite supports a small deployment with multiple worker processes on one shared
filesystem. Production multi-node installations should use PostgreSQL/Temporal.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid

from agentic.schemas import WorkflowIn
from agentic.admission import safe_metadata


class BudgetExceeded(ValueError):
    """A request was stopped before exceeding the configured reservation budget."""


class QueueFull(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


def deadline_result() -> dict:
    return {"outcome": "deadline", "stop_reason": "step_timeout",
            "answer": "Execution stopped at the configured deadline.", "citations": []}


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, goal TEXT NOT NULL,
 status TEXT NOT NULL, idempotency_key TEXT, request_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 specification TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency
 ON runs(tenant,idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS runs_tenant ON runs(tenant,created_at);
CREATE TABLE IF NOT EXISTS steps (
 run_id TEXT NOT NULL, id TEXT NOT NULL, tool TEXT NOT NULL,
 args TEXT NOT NULL, depends_on TEXT NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 lease_until REAL, lease_token TEXT, approved INTEGER NOT NULL DEFAULT 0,
 approval_required INTEGER NOT NULL DEFAULT 0,
 result TEXT, error TEXT, checkpoint TEXT NOT NULL DEFAULT '{}',
 ordinal INTEGER NOT NULL DEFAULT 0,
 deadline_at REAL,
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS steps_queue ON steps(status,available_at);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, step_id TEXT,
 type TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE TABLE IF NOT EXISTS definitions (
 tenant TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
 version INTEGER NOT NULL, definition TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(tenant,kind,name,version)
);
CREATE TABLE IF NOT EXISTS memories (
 tenant TEXT NOT NULL, namespace TEXT NOT NULL, id TEXT NOT NULL,
 content TEXT NOT NULL, metadata TEXT NOT NULL, source_run_id TEXT,
 expires_at REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(tenant,namespace,id)
);
CREATE INDEX IF NOT EXISTS memories_namespace ON memories(tenant,namespace,expires_at);
CREATE TABLE IF NOT EXISTS model_usage (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, run_id TEXT, route TEXT NOT NULL,
 purpose TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
 charged_tokens INTEGER NOT NULL, cost_microusd INTEGER, status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS usage_run ON model_usage(tenant,run_id);
CREATE TABLE IF NOT EXISTS artifact_refs (
 tenant TEXT NOT NULL, artifact_id TEXT NOT NULL, run_id TEXT NOT NULL,
 namespace TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(tenant,artifact_id,run_id), FOREIGN KEY(run_id) REFERENCES runs(id)
);
"""


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(
        self, path: str, *, lease_seconds: float = 180, max_attempts: int = 3,
        step_timeout_seconds: float = 300,
    ):
        self.path = path
        self.is_postgres = path.startswith(("postgresql://", "postgres://"))
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.step_timeout_seconds = step_timeout_seconds
        if path != ":memory:" and not self.is_postgres:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Additive migration preserves v0.1 runs and their original step order.
            run_columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            step_columns = {r[1] for r in conn.execute("PRAGMA table_info(steps)")}
            if "specification" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN specification TEXT NOT NULL DEFAULT '{}'")
            if "checkpoint" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN checkpoint TEXT NOT NULL DEFAULT '{}'")
            if "ordinal" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE steps SET ordinal=rowid")
            if "deadline_at" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN deadline_at REAL")
            if self.is_postgres:
                from agentic.postgres import migrate_accounting
                migrate_accounting(conn)

    @contextmanager
    def connect(self, *, read_only: bool = False, serialized: bool = True) -> Iterator[sqlite3.Connection]:
        if self.is_postgres:
            from agentic.postgres import connect
            with connect(self.path, read_only=read_only, serialized=serialized) as conn:
                yield conn
            return
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=20000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            if read_only:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _run_connection(self, run_id: str | None, *, tenant: str | None = None):
        """Acquire workflow state in the invariant order: run before children."""
        if getattr(self, "is_postgres", False) and run_id is not None:
            with self.connect(serialized=False) as conn:
                query = "SELECT id FROM runs WHERE id=?" + (" AND tenant=?" if tenant is not None else "")
                conn.execute(query + " FOR UPDATE", (run_id, tenant) if tenant is not None else (run_id,)).fetchone()
                yield conn
        else:
            with self.connect() as conn:
                yield conn

    @staticmethod
    def event(conn, tenant: str, run_id: str, step_id: str | None, kind: str, data: dict) -> None:
        conn.execute(
            "INSERT INTO events(timestamp,tenant,run_id,step_id,type,payload)"
            " VALUES(?,?,?,?,?,?)",
            (stamp(), tenant, run_id, step_id, kind, json.dumps(data)),
        )

    @staticmethod
    def refresh(conn, run_id: str) -> None:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] in {"cancelled", "completed", "failed"}:
            return
        states = [
            r["status"] for r in conn.execute(
                "SELECT status FROM steps WHERE run_id=?", (run_id,)
            )
        ]
        if "failed" in states:
            status = "failed"
        elif states and all(s == "completed" for s in states):
            status = "completed"
        elif "waiting_approval" in states and "leased" not in states:
            status = "waiting_approval"
        else:
            status = "running"
        conn.execute(
            "UPDATE runs SET status=?,updated_at=? WHERE id=?", (status, stamp(), run_id)
        )

    def submit(
        self, tenant: str, data: WorkflowIn, *, specification: dict | None = None,
        request_hash: str | None = None, max_queued_runs: int = 100,
    ) -> dict:
        assert data.steps is not None
        payload = json.dumps(
            {"goal": data.goal, "steps": [s.model_dump() for s in data.steps]},
            sort_keys=True,
        )
        signature = request_hash or hashlib.sha256(payload.encode()).hexdigest()
        run_id = str(uuid.uuid4())
        created = stamp()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if data.idempotency_key:
                row = conn.execute(
                    "SELECT id,request_hash FROM runs WHERE tenant=? AND idempotency_key=?",
                    (tenant, data.idempotency_key),
                ).fetchone()
                if row:
                    if row["request_hash"] != signature:
                        raise ValueError("Idempotency key already used with a different workflow")
                    return {"id": row["id"], "duplicate": True}
            queued = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE tenant=? "
                "AND status NOT IN ('completed','failed','cancelled')", (tenant,),
            ).fetchone()[0]
            if queued >= max_queued_runs:
                raise QueueFull("Tenant workflow queue is full")
            conn.execute(
                "INSERT INTO runs(id,tenant,goal,status,idempotency_key,request_hash,"
                "created_at,updated_at,specification) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, tenant, data.goal, "queued", data.idempotency_key,
                 signature, created, created, json.dumps(specification or {})),
            )
            for ordinal, step in enumerate(data.steps):
                conn.execute(
                    "INSERT INTO steps(run_id,id,tool,args,depends_on,status,"
                    "approval_required,ordinal) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, step.id, step.tool, json.dumps(step.args),
                     json.dumps(step.depends_on), "pending",
                     int(step.approval_required), ordinal),
                )
            self.event(conn, tenant, run_id, None, "created", {"steps": len(data.steps)})
        return {"id": run_id, "duplicate": False}

    def get(self, tenant: str, run_id: str) -> dict | None:
        with self.connect(read_only=True) as conn:
            run = conn.execute(
                "SELECT * FROM runs WHERE tenant=? AND id=?", (tenant, run_id)
            ).fetchone()
            if not run:
                return None
            steps = conn.execute(
                "SELECT id,tool,args,depends_on,status,attempts,approval_required,"
                "approved,result,error,checkpoint,deadline_at FROM steps WHERE run_id=? ORDER BY ordinal,id", (run_id,)
            ).fetchall()
            return {
                **dict(run),
                "specification": json.loads(run["specification"]),
                "steps": [
                    {
                        **dict(s),
                        "args": json.loads(s["args"]),
                        "depends_on": json.loads(s["depends_on"]),
                        "result": json.loads(s["result"]) if s["result"] else None,
                        "checkpoint": json.loads(s["checkpoint"]),
                        "approval_required": bool(s["approval_required"]),
                        "approved": bool(s["approved"]),
                    }
                    for s in steps
                ],
            }

    def list(self, tenant: str, limit: int = 50) -> list[dict]:
        with self.connect(read_only=True) as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT id,goal,status,created_at,updated_at FROM runs "
                    "WHERE tenant=? ORDER BY created_at DESC LIMIT ?", (tenant, limit)
                )
            ]

    def events(self, tenant: str, run_id: str, after_id: int = 0, limit: int = 1000) -> list[dict]:
        with self.connect(read_only=True) as conn:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in conn.execute(
                    "SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id "
                    "WHERE e.tenant=? AND e.run_id=? AND r.tenant=? AND e.id>? ORDER BY e.id LIMIT ?",
                    (tenant, run_id, tenant, after_id, min(1000, max(1, limit))),
                )
            ]

    def claim(self) -> dict | None:
        if self.is_postgres:
            return self._claim_postgres()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._claim_locked(conn, time.time())

    def _claim_postgres(self) -> dict | None:
        # A run lock coordinates claims with cancellation, completion and budgets.
        # Dependency checks in SQL avoid scanning every blocked pending step. Each
        # housekeeping-only transaction commits before selecting another run.
        while True:
            with self.connect(serialized=False) as conn:
                clock = time.time()
                row = conn.execute(
                    "SELECT r.id FROM runs r WHERE r.status NOT IN ('completed','cancelled','failed') "
                    "AND EXISTS (SELECT 1 FROM steps s WHERE s.run_id=r.id AND ("
                    "(s.status='leased' AND s.lease_until<?) OR "
                    "(s.status='pending' AND s.available_at<=? AND ("
                    "(s.deadline_at IS NOT NULL AND s.deadline_at<=?) OR NOT EXISTS ("
                    "SELECT 1 FROM jsonb_array_elements_text(s.depends_on::jsonb) AS dep(id) "
                    "LEFT JOIN steps d ON d.run_id=s.run_id AND d.id=dep.id "
                    "WHERE d.status IS DISTINCT FROM 'completed'))))) "
                    "ORDER BY r.created_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                    (clock, clock, clock),
                ).fetchone()
                if row is None:
                    return None
                job = self._claim_locked(conn, clock, row["id"])
            if job is not None:
                return job

    def _claim_locked(self, conn, clock: float, run_id: str | None = None) -> dict | None:
        expired = conn.execute(
            "SELECT s.run_id,s.id,s.attempts,r.tenant FROM steps s "
            "JOIN runs r ON r.id=s.run_id "
            "WHERE s.status='leased' AND s.lease_until<? "
            "AND r.status NOT IN ('cancelled','failed','completed')"
            + (" AND s.run_id=?" if run_id is not None else ""),
            (clock, run_id) if run_id is not None else (clock,),
        ).fetchall()
        for row in expired:
            exhausted = row["attempts"] >= self.max_attempts
            conn.execute(
                "UPDATE steps SET status=?,error=?,lease_token=NULL,lease_until=NULL "
                "WHERE run_id=? AND id=?",
                ("failed" if exhausted else "pending", "Lease recovery limit reached" if exhausted else None,
                 row["run_id"], row["id"]),
            )
            self.event(conn, row["tenant"], row["run_id"], row["id"],
                       "lease_exhausted" if exhausted else "lease_expired", {})
            self.refresh(conn, row["run_id"])
        rows = conn.execute(
            "SELECT s.*,r.tenant,r.goal,r.specification FROM steps s JOIN runs r ON r.id=s.run_id "
            "WHERE s.status='pending' AND s.available_at<=? "
            "AND r.status NOT IN ('completed','cancelled','failed') "
            + ("AND s.run_id=? " if run_id is not None else "")
            + "ORDER BY r.created_at,s.ordinal,s.id",
            (clock, run_id) if run_id is not None else (clock,)
        ).fetchall()
        stopped = set()
        for row in rows:
            if row["run_id"] in stopped:
                continue
            if row["deadline_at"] is not None and row["deadline_at"] <= clock:
                result = {"outcome": "deadline", "stop_reason": "persisted_step_deadline"}
                conn.execute(
                    "UPDATE steps SET status='failed',error='deadline',result=? WHERE run_id=? AND id=?",
                    (json.dumps(result), row["run_id"], row["id"]),
                )
                self.event(conn, row["tenant"], row["run_id"], row["id"], "deadline", {})
                self.refresh(conn, row["run_id"])
                stopped.add(row["run_id"])
                continue
            depends = json.loads(row["depends_on"])
            if depends:
                placeholders = ",".join("?" for _ in depends)
                done = conn.execute(
                    "SELECT COUNT(*) FROM steps WHERE run_id=? AND status='completed' "
                    f"AND id IN ({placeholders})", [row["run_id"], *depends]
                ).fetchone()[0]
                if done != len(depends):
                    continue
            if row["approval_required"] and not row["approved"]:
                conn.execute(
                    "UPDATE steps SET status='waiting_approval' WHERE run_id=? AND id=?",
                    (row["run_id"], row["id"]),
                )
                self.event(conn, row["tenant"], row["run_id"], row["id"],
                           "approval_requested", {})
                self.refresh(conn, row["run_id"])
                continue
            token = uuid.uuid4().hex
            deadline = row["deadline_at"] or clock + self.step_timeout_seconds
            conn.execute(
                "UPDATE steps SET status='leased',attempts=attempts+1,"
                "lease_token=?,lease_until=?,deadline_at=? WHERE run_id=? AND id=?",
                (token, clock + self.lease_seconds, deadline, row["run_id"], row["id"]),
            )
            conn.execute(
                "UPDATE runs SET status='running',updated_at=? WHERE id=?",
                (stamp(), row["run_id"]),
            )
            self.event(conn, row["tenant"], row["run_id"], row["id"], "started",
                       {"attempt": row["attempts"] + 1})
            return {
                **dict(row), "lease_token": token,
                "depends_on": depends, "args": json.loads(row["args"]),
                "checkpoint": json.loads(row["checkpoint"]),
                "specification": json.loads(row["specification"]),
                "deadline_at": deadline,
            }
        return None

    def input_results(self, run_id: str, dependencies: list[str]) -> dict:
        if not dependencies:
            return {}
        with self.connect(read_only=True) as conn:
            placeholders = ",".join("?" for _ in dependencies)
            rows = conn.execute(
                f"SELECT id,result FROM steps WHERE run_id=? AND id IN ({placeholders})",
                [run_id, *dependencies],
            ).fetchall()
            return {r["id"]: json.loads(r["result"]) for r in rows if r["result"]}

    def complete(self, job: dict, result: dict) -> bool:
        serialized = json.dumps(result)
        if len(serialized) > 100_000:
            raise ValueError("Tool output exceeds 100KB")
        outcome = result.get("outcome", "success")
        unsuccessful = outcome in {"insufficient_evidence", "budget_exhausted", "deadline", "policy_denied"}
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            # SQLite must acquire its write lock before evaluating time. The
            # PostgreSQL run lock is already held by _run_connection.
            conn.execute("BEGIN IMMEDIATE")
            if not self._owns(conn, job):
                return False
            cur = conn.execute(
                "UPDATE steps SET status=?,result=?,error=?,"
                "lease_token=NULL,lease_until=NULL WHERE run_id=? AND id=? "
                "AND status='leased' AND lease_token=? AND lease_until>? "
                "AND EXISTS (SELECT 1 FROM runs WHERE id=? AND tenant=? "
                "AND status NOT IN ('cancelled','failed','completed'))",
                ("failed" if unsuccessful else "completed", serialized, outcome if unsuccessful else None,
                 job["run_id"], job["id"], job["lease_token"], time.time(), job["run_id"], job["tenant"]),
            )
            if cur.rowcount == 0:
                return False  # stale worker or cancelled run; fenced out
            self.event(conn, job["tenant"], job["run_id"], job["id"],
                       "failed" if unsuccessful else "completed", {
                           "result_bytes": len(serialized), "outcome": outcome,
                           **safe_metadata(result, job.get("specification", {}).get("rag_admission_profile_ids", [])),
                       })
            self.refresh(conn, job["run_id"])
            return True

    def fail(self, job: dict, error: str, maximum: int | None = None, *, terminal: bool = False) -> bool:
        attempts = job["attempts"] + 1
        final = terminal or attempts >= (maximum or self.max_attempts)
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._owns(conn, job):
                return False
            cur = conn.execute(
                "UPDATE steps SET status=?,error=?,available_at=?,"
                "lease_token=NULL,lease_until=NULL WHERE run_id=? AND id=? "
                "AND status='leased' AND lease_token=? AND lease_until>? "
                "AND EXISTS (SELECT 1 FROM runs WHERE id=? AND tenant=? "
                "AND status NOT IN ('cancelled','failed','completed'))",
                ("failed" if final else "pending", error[:500],
                 time.time() + min(30, 2**attempts), job["run_id"],
                 job["id"], job["lease_token"], time.time(), job["run_id"], job["tenant"]),
            )
            if cur.rowcount == 0:
                return False
            self.event(conn, job["tenant"], job["run_id"], job["id"],
                       "failed" if final else "retry_scheduled",
                       {"attempt": attempts, "error": error[:200]})
            self.refresh(conn, job["run_id"])
            return True

    def approve(self, tenant: str, run_id: str, step_id: str, approved: bool, reason: str) -> bool:
        if type(approved) is not bool:
            raise ValueError("Approval decision must be boolean")
        with self._run_connection(run_id, tenant=tenant) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT id,status FROM runs WHERE id=? AND tenant=?", (run_id, tenant)
            ).fetchone()
            if not run or run["status"] in {"failed", "cancelled", "completed"}:
                return False
            status = "pending" if approved else "failed"
            cur = conn.execute(
                "UPDATE steps SET status=?,approved=?,error=? WHERE run_id=? "
                "AND id=? AND status='waiting_approval'",
                (status, int(approved), None if approved else reason or "Rejected by operator",
                 run_id, step_id),
            )
            if not cur.rowcount:
                return False
            self.event(conn, tenant, run_id, step_id,
                       "approved" if approved else "rejected", {"reason_provided": bool(reason)})
            self.refresh(conn, run_id)
            return True

    def cancel(self, tenant: str, run_id: str) -> bool:
        with self._run_connection(run_id, tenant=tenant) as conn:
            cur = conn.execute(
                "UPDATE runs SET status='cancelled',updated_at=? "
                "WHERE tenant=? AND id=? AND status NOT IN ('completed','failed','cancelled')",
                (stamp(), tenant, run_id),
            )
            if cur.rowcount:
                self.event(conn, tenant, run_id, None, "cancelled", {})
            return cur.rowcount > 0

    @staticmethod
    def fingerprint(data: WorkflowIn) -> str:
        body = json.dumps(data.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def duplicate(self, tenant: str, data: WorkflowIn) -> dict | None:
        if not data.idempotency_key:
            return None
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT id,request_hash FROM runs WHERE tenant=? AND idempotency_key=?",
                (tenant, data.idempotency_key),
            ).fetchone()
            if row:
                if row["request_hash"] != self.fingerprint(data):
                    raise ValueError("Idempotency key already used with a different workflow")
                return {"id": row["id"], "duplicate": True}
        return None

    def _owns(self, conn, job: dict) -> bool:
        """Fence execution after write-lock acquisition, finalizing expiry.

        Callers must let the transaction commit when this returns False. An
        expired but still-owned lease records its terminal deadline, whereas a
        stale lease or cancelled run cannot mutate anything.
        """
        row = conn.execute(
            "SELECT s.deadline_at,s.lease_until FROM steps s JOIN runs r ON r.id=s.run_id "
            "WHERE s.run_id=? AND s.id=? AND s.status='leased' "
            "AND s.lease_token=? AND r.tenant=? "
            "AND r.status NOT IN ('completed','failed','cancelled')",
            (job["run_id"], job["id"], job["lease_token"], job["tenant"]),
        ).fetchone()
        clock = time.time()
        if row is None or row["lease_until"] <= clock:
            return False
        if ((row["deadline_at"] is not None and row["deadline_at"] <= clock)
                or job.get("worker_deadline_at", float("inf")) <= clock):
            serialized = json.dumps(deadline_result())
            conn.execute(
                "UPDATE steps SET status='failed',result=?,error='deadline',lease_token=NULL,lease_until=NULL "
                "WHERE run_id=? AND id=?", (serialized, job["run_id"], job["id"]),
            )
            self.event(conn, job["tenant"], job["run_id"], job["id"], "failed",
                       {"result_bytes": len(serialized), "outcome": "deadline"})
            self.refresh(conn, job["run_id"])
            return False
        return True

    def heartbeat(self, job: dict) -> bool:
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._owns(conn, job):
                return False
            conn.execute(
                "UPDATE steps SET lease_until=? WHERE run_id=? AND id=? AND lease_token=?",
                (time.time() + self.lease_seconds, job["run_id"], job["id"], job["lease_token"]),
            )
            return True

    def checkpoint(self, job: dict, state: dict, *, event: str | None = None, details: dict | None = None) -> bool:
        serialized = json.dumps(state)
        if len(serialized) > 350000:
            raise ValueError("Runtime checkpoint exceeds limit")
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._owns(conn, job):
                return False
            conn.execute(
                "UPDATE steps SET checkpoint=? WHERE run_id=? AND id=? AND lease_token=?",
                (serialized, job["run_id"], job["id"], job["lease_token"]),
            )
            if event:
                self.event(conn, job["tenant"], job["run_id"], job["id"], event, details or {})
            return True

    def expand_plan(self, job: dict, steps: list) -> list[str]:
        """Install a generated DAG atomically with the planner checkpoint."""
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._owns(conn, job):
                row = conn.execute(
                    "SELECT checkpoint FROM steps WHERE run_id=? AND id=?", (job["run_id"], job["id"]),
                ).fetchone()
                checkpoint = json.loads(row["checkpoint"])
                if checkpoint.get("expanded"):
                    return checkpoint["step_ids"]
                names = {step.id: f"p{index}_{step.id[:48]}" for index, step in enumerate(steps)}
                for ordinal, step in enumerate(steps, 1):
                    conn.execute(
                        "INSERT INTO steps(run_id,id,tool,args,depends_on,status,approval_required,ordinal) "
                        "VALUES(?,?,?,?,?,'pending',?,?)",
                        (job["run_id"], names[step.id], step.tool, json.dumps(step.args),
                         json.dumps([names[d] for d in step.depends_on] + [job["id"]]),
                         int(step.approval_required), ordinal),
                    )
                ids = list(names.values())
                conn.execute(
                    "UPDATE steps SET checkpoint=? WHERE run_id=? AND id=?",
                    (json.dumps({"expanded": True, "step_ids": ids}), job["run_id"], job["id"]),
                )
                self.event(conn, job["tenant"], job["run_id"], job["id"], "plan_installed", {"steps": len(ids)})
                return ids
        # Expiry is a committed outcome, so never raise inside its transaction.
        raise LeaseLost("Planner lease or execution deadline is no longer active")

    def register(self, tenant: str, kind: str, definition: dict) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM definitions WHERE tenant=? AND kind=? AND name=? AND version=?",
                (tenant, kind, definition["name"], definition["version"]),
            ).fetchone()
            if exists:
                raise ValueError("Definition versions are immutable")
            conn.execute(
                "INSERT INTO definitions(tenant,kind,name,version,definition,created_at) VALUES(?,?,?,?,?,?)",
                (tenant, kind, definition["name"], definition["version"], json.dumps(definition), stamp()),
            )
        return definition

    def definition(self, tenant: str, kind: str, name: str, version: int | None = None) -> dict | None:
        with self.connect(read_only=True) as conn:
            sql = "SELECT definition FROM definitions WHERE tenant=? AND kind=? AND name=?"
            args = [tenant, kind, name]
            if version is not None:
                sql += " AND version=?"
                args.append(version)
            row = conn.execute(sql + " ORDER BY version DESC LIMIT 1", args).fetchone()
            return json.loads(row["definition"]) if row else None

    def definitions(self, tenant: str, kind: str) -> list[dict]:
        with self.connect(read_only=True) as conn:
            return [json.loads(row["definition"]) for row in conn.execute(
                "SELECT definition FROM definitions WHERE tenant=? AND kind=? "
                "ORDER BY name,version DESC LIMIT 300", (tenant, kind),
            )]

    def put_memory(self, tenant: str, namespace: str, record_id: str, record) -> dict:
        now = stamp()
        expires = time.time() + record.ttl_seconds if record.ttl_seconds else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if record.source_run_id and conn.execute(
                "SELECT 1 FROM runs WHERE tenant=? AND id=?", (tenant, record.source_run_id),
            ).fetchone() is None:
                raise ValueError("Memory provenance must reference this tenant's workflow")
            conn.execute(
                "INSERT INTO memories(tenant,namespace,id,content,metadata,source_run_id,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,namespace,id) DO UPDATE SET "
                "content=excluded.content,metadata=excluded.metadata,source_run_id=excluded.source_run_id,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (tenant, namespace, record_id, record.content, json.dumps(record.metadata),
                 record.source_run_id, expires, now, now),
            )
        return self.memory(tenant, namespace, record_id)

    @staticmethod
    def _memory_row(row) -> dict:
        return {**dict(row), "metadata": json.loads(row["metadata"])}

    def memory(self, tenant: str, namespace: str, record_id: str) -> dict | None:
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE tenant=? AND namespace=? AND id=? "
                "AND (expires_at IS NULL OR expires_at>?)", (tenant, namespace, record_id, time.time()),
            ).fetchone()
            return self._memory_row(row) if row else None

    def memory_candidates(self, tenant: str, namespace: str, terms: list[str]) -> list[dict]:
        sql = "SELECT * FROM memories WHERE tenant=? AND namespace=? AND (expires_at IS NULL OR expires_at>?)"
        args: list = [tenant, namespace, time.time()]
        if terms:
            sql += " AND (" + " OR ".join("LOWER(content) LIKE ?" for _ in terms) + ")"
            args.extend("%" + term.replace("%", "").replace("_", "") + "%" for term in terms)
        sql += " ORDER BY updated_at DESC,id LIMIT 500"
        with self.connect(read_only=True) as conn:
            return [self._memory_row(row) for row in conn.execute(sql, args)]

    def delete_memory(self, tenant: str, namespace: str, record_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "DELETE FROM memories WHERE tenant=? AND namespace=? AND id=?", (tenant, namespace, record_id),
            ).rowcount > 0

    def remaining_model_tokens(self, tenant: str, run_id: str | None, default_limit: int) -> int:
        """Packing hint only; the following atomic reservation is authoritative."""
        if run_id is None:
            return default_limit
        with self.connect(read_only=True) as db:
            row = db.execute("SELECT specification,status FROM runs WHERE tenant=? AND id=?", (tenant, run_id)).fetchone()
            if row is None or row["status"] in {"failed", "completed", "cancelled"}:
                raise LeaseLost("Model packing requires an active tenant workflow")
            specification = json.loads(row["specification"])
            used = int(db.execute("SELECT COALESCE(SUM(charged_tokens),0) FROM model_usage WHERE tenant=? AND run_id=?",
                                  (tenant, run_id)).fetchone()[0])
            remaining = specification.get("max_model_tokens", default_limit) - used
            if remaining <= 0:
                raise BudgetExceeded("Workflow model budget is exhausted")
            return remaining

    def reserve_usage(
        self, tenant: str, run_id: str | None, route: str, purpose: str,
        tokens: int, cost_microusd: int | None, *, default_limit: int = 12000,
    ) -> str:
        reservation = uuid.uuid4().hex
        with self._run_connection(run_id, tenant=tenant) as conn:
            conn.execute("BEGIN IMMEDIATE")
            specification = {}
            if run_id:
                row = conn.execute(
                    "SELECT specification,status FROM runs WHERE tenant=? AND id=?", (tenant, run_id),
                ).fetchone()
                if row is None or row["status"] in {"failed", "completed", "cancelled"}:
                    raise LeaseLost("Model request requires an active tenant workflow")
                specification = json.loads(row["specification"])
                usage = conn.execute(
                    "SELECT COALESCE(SUM(charged_tokens),0) AS spent_tokens,"
                    "COALESCE(SUM(cost_microusd),0) AS spent_cost "
                    "FROM model_usage WHERE tenant=? AND run_id=?", (tenant, run_id),
                ).fetchone()
                spent_tokens, spent_cost = int(usage["spent_tokens"]), int(usage["spent_cost"])
            else:
                spent_tokens, spent_cost = 0, 0
            budget = specification.get("max_model_tokens", default_limit)
            cost_budget = specification.get("max_cost_usd")
            if tokens < 0 or spent_tokens + tokens > budget:
                raise BudgetExceeded("Token reservation would exceed the workflow budget")
            if cost_budget is not None and (
                cost_microusd is None or spent_cost + cost_microusd > math.floor(cost_budget * 1_000_000)
            ):
                raise BudgetExceeded("Cost reservation requires prices and sufficient workflow budget")
            conn.execute(
                "INSERT INTO model_usage(id,tenant,run_id,route,purpose,charged_tokens,cost_microusd,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,'reserved',?)",
                (reservation, tenant, run_id, route, purpose, tokens, cost_microusd, stamp()),
            )
            if run_id:
                self.event(conn, tenant, run_id, None, "model_reserved", {"route": route, "purpose": purpose})
        return reservation

    def settle_usage(
        self, reservation: str, *, input_tokens: int | None = None,
        output_tokens: int | None = None, cost_microusd: int | None = None, status: str = "reported",
    ) -> None:
        with self.connect(read_only=True) as conn:
            owner = conn.execute("SELECT run_id,tenant FROM model_usage WHERE id=?", (reservation,)).fetchone()
        if owner is None:
            return
        with self._run_connection(owner["run_id"], tenant=owner["tenant"]) as conn:
            # The reserved-state check, final charge and event are one transition.
            # Retried or concurrent settlement must preserve the first result.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM model_usage WHERE id=?", (reservation,)).fetchone()
            if not row or row["status"] != "reserved":
                return
            reported = input_tokens is not None and output_tokens is not None
            conn.execute(
                "UPDATE model_usage SET input_tokens=?,output_tokens=?,charged_tokens=?,cost_microusd=?,status=? WHERE id=?",
                (input_tokens, output_tokens, input_tokens + output_tokens if reported else row["charged_tokens"],
                 cost_microusd if reported else row["cost_microusd"], status, reservation),
            )
            if row["run_id"]:
                self.event(conn, row["tenant"], row["run_id"], None, "model_usage", {
                    "route": row["route"], "purpose": row["purpose"], "status": status,
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                })

    def usage(self, tenant: str, run_id: str | None = None, *, namespaces: list[str] | None = None) -> dict:
        sql = "SELECT model_usage.* FROM model_usage WHERE model_usage.tenant=?"
        args = [tenant]
        if run_id is not None:
            sql += " AND model_usage.run_id=?"
            args.append(run_id)
        if namespaces is not None:
            # Bind namespace values in SQL before returning records or totals.
            # Unbound usage has no workflow namespace and is excluded. Both
            # sides of the join are tenant-scoped, including legacy default runs.
            if namespaces:
                namespace = ("CASE WHEN jsonb_exists(r.specification::jsonb,'memory_namespace') "
                             "THEN r.specification::jsonb->>'memory_namespace' ELSE 'default' END"
                             if self.is_postgres else
                             "CASE WHEN json_type(r.specification,'$.memory_namespace') IS NULL "
                             "THEN 'default' ELSE json_extract(r.specification,'$.memory_namespace') END")
                sql += (" AND EXISTS (SELECT 1 FROM runs r WHERE r.id=model_usage.run_id "
                        "AND r.tenant=model_usage.tenant AND " + namespace + " IN (" +
                        ",".join("?" for _ in namespaces) + "))")
                args.extend(namespaces)
            else:
                sql += " AND 1=0"
        with self.connect(read_only=True) as conn:
            records = [dict(row) for row in conn.execute(sql + " ORDER BY model_usage.created_at,model_usage.id", args)]
        known_cost = sum(row["cost_microusd"] or 0 for row in records) / 1_000_000
        cost_complete = all(row["cost_microusd"] is not None for row in records)
        return {
            "calls": records, "charged_tokens": sum(row["charged_tokens"] for row in records),
            "estimated_cost_usd": known_cost if cost_complete else None,
            "known_cost_usd": known_cost, "cost_complete": cost_complete,
            "usage_complete": all(row["status"] == "reported" for row in records),
        }

    def register_artifact(self, job: dict, reference: dict) -> bool:
        with self._run_connection(job["run_id"], tenant=job["tenant"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._owns(conn, job):
                return False
            conn.execute(
                "INSERT INTO artifact_refs(tenant,artifact_id,run_id,namespace,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tenant,artifact_id,run_id) DO NOTHING",
                (job["tenant"], reference["artifact_id"], job["run_id"],
                 job.get("specification", {}).get("memory_namespace", "default"), stamp()),
            )
            return True

    def artifact_namespaces(self, tenant: str, artifact_id: str) -> list[str]:
        with self.connect(read_only=True) as conn:
            return [row["namespace"] for row in conn.execute(
                "SELECT namespace FROM artifact_refs WHERE tenant=? AND artifact_id=?", (tenant, artifact_id),
            )]
