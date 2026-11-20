"""Durable workflow storage with leases, fencing tokens and append-only events.

SQLite supports a small deployment with multiple worker processes on one shared
filesystem. Production multi-node installations should use PostgreSQL/Temporal.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid

from agentic.schemas import WorkflowIn


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, goal TEXT NOT NULL,
 status TEXT NOT NULL, idempotency_key TEXT, request_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
 result TEXT, error TEXT,
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
"""


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

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

    def submit(self, tenant: str, data: WorkflowIn) -> dict:
        assert data.steps is not None
        payload = json.dumps(
            {"goal": data.goal, "steps": [s.model_dump() for s in data.steps]},
            sort_keys=True,
        )
        signature = hashlib.sha256(payload.encode()).hexdigest()
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
            conn.execute(
                "INSERT INTO runs(id,tenant,goal,status,idempotency_key,request_hash,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, tenant, data.goal, "queued", data.idempotency_key,
                 signature, created, created),
            )
            for step in data.steps:
                conn.execute(
                    "INSERT INTO steps(run_id,id,tool,args,depends_on,status,"
                    "approval_required) VALUES(?,?,?,?,?,?,?)",
                    (run_id, step.id, step.tool, json.dumps(step.args),
                     json.dumps(step.depends_on), "pending",
                     int(step.approval_required)),
                )
            self.event(conn, tenant, run_id, None, "created", {"steps": len(data.steps)})
        return {"id": run_id, "duplicate": False}

    def get(self, tenant: str, run_id: str) -> dict | None:
        with self.connect() as conn:
            run = conn.execute(
                "SELECT * FROM runs WHERE tenant=? AND id=?", (tenant, run_id)
            ).fetchone()
            if not run:
                return None
            steps = conn.execute(
                "SELECT id,tool,args,depends_on,status,attempts,approval_required,"
                "approved,result,error FROM steps WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
            return {
                **dict(run),
                "steps": [
                    {
                        **dict(s),
                        "args": json.loads(s["args"]),
                        "depends_on": json.loads(s["depends_on"]),
                        "result": json.loads(s["result"]) if s["result"] else None,
                        "approval_required": bool(s["approval_required"]),
                        "approved": bool(s["approved"]),
                    }
                    for s in steps
                ],
            }

    def list(self, tenant: str, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT id,goal,status,created_at,updated_at FROM runs "
                    "WHERE tenant=? ORDER BY created_at DESC LIMIT ?", (tenant, limit)
                )
            ]

    def events(self, tenant: str, run_id: str) -> list[dict]:
        with self.connect() as conn:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in conn.execute(
                    "SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id "
                    "WHERE e.tenant=? AND e.run_id=? AND r.tenant=? ORDER BY e.id",
                    (tenant, run_id, tenant),
                )
            ]

    def claim(self) -> dict | None:
        clock = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = conn.execute(
                "SELECT s.run_id,s.id,r.tenant FROM steps s "
                "JOIN runs r ON r.id=s.run_id "
                "WHERE s.status='leased' AND s.lease_until<? "
                "AND r.status NOT IN ('cancelled','failed','completed')",
                (clock,),
            ).fetchall()
            for row in expired:
                conn.execute(
                    "UPDATE steps SET status='pending',lease_token=NULL,lease_until=NULL "
                    "WHERE run_id=? AND id=?", (row["run_id"], row["id"])
                )
                self.event(conn, row["tenant"], row["run_id"], row["id"], "lease_expired", {})
            rows = conn.execute(
                "SELECT s.*,r.tenant,r.goal FROM steps s JOIN runs r ON r.id=s.run_id "
                "WHERE s.status='pending' AND s.available_at<=? "
                "AND r.status NOT IN ('completed','cancelled','failed') "
                "ORDER BY r.created_at,s.rowid LIMIT 200", (clock,)
            ).fetchall()
            for row in rows:
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
                conn.execute(
                    "UPDATE steps SET status='leased',attempts=attempts+1,"
                    "lease_token=?,lease_until=? WHERE run_id=? AND id=?",
                    (token, clock + 180, row["run_id"], row["id"]),
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
                }
            return None

    def input_results(self, run_id: str, dependencies: list[str]) -> dict:
        if not dependencies:
            return {}
        with self.connect() as conn:
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
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE steps SET status='completed',result=?,error=NULL,"
                "lease_token=NULL,lease_until=NULL WHERE run_id=? AND id=? "
                "AND status='leased' AND lease_token=? "
                "AND EXISTS (SELECT 1 FROM runs WHERE id=? "
                "AND status NOT IN ('cancelled','failed','completed'))",
                (serialized, job["run_id"], job["id"], job["lease_token"], job["run_id"]),
            )
            if cur.rowcount == 0:
                return False  # stale worker or cancelled run; fenced out
            self.event(conn, job["tenant"], job["run_id"], job["id"],
                       "completed", {"result_bytes": len(serialized)})
            self.refresh(conn, job["run_id"])
            return True

    def fail(self, job: dict, error: str, maximum: int = 3) -> bool:
        attempts = job["attempts"] + 1
        final = attempts >= maximum
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE steps SET status=?,error=?,available_at=?,"
                "lease_token=NULL,lease_until=NULL WHERE run_id=? AND id=? "
                "AND status='leased' AND lease_token=?",
                ("failed" if final else "pending", error[:500],
                 time.time() + min(30, 2**attempts), job["run_id"],
                 job["id"], job["lease_token"]),
            )
            if cur.rowcount == 0:
                return False
            self.event(conn, job["tenant"], job["run_id"], job["id"],
                       "failed" if final else "retry_scheduled",
                       {"attempt": attempts, "error": error[:200]})
            self.refresh(conn, job["run_id"])
            return True

    def approve(self, tenant: str, run_id: str, step_id: str, approved: bool, reason: str) -> bool:
        with self.connect() as conn:
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
                       "approved" if approved else "rejected", {"reason": reason})
            self.refresh(conn, run_id)
            return True

    def cancel(self, tenant: str, run_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='cancelled',updated_at=? "
                "WHERE tenant=? AND id=? AND status NOT IN ('completed','failed','cancelled')",
                (stamp(), tenant, run_id),
            )
            if cur.rowcount:
                self.event(conn, tenant, run_id, None, "cancelled", {})
            return cur.rowcount > 0
