"""Real filesystem integrity, atomic replacement and shared SQLite rate limits."""
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agentic.auth import RateLimiter
from agentic.config import secret
from agentic.storage_adapters import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ARTIFACT_BACKEND", "local")
    monkeypatch.setenv("AGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    return ArtifactStore()


def test_artifact_identity_is_canonical_and_tenant_paths_do_not_expose_names(artifacts):
    value = {"total": 0, "supported": False, "source": "中文"}
    reference = artifacts.put("private-tenant", value)
    duplicate = artifacts.put("private-tenant", {"source": "中文", "supported": False, "total": 0})
    assert duplicate == reference
    assert artifacts.get("private-tenant", reference["artifact_id"]) == value
    assert artifacts.get("other-tenant", reference["artifact_id"]) is None
    paths = list(artifacts.root.rglob("*.json"))
    assert len(paths) == 1 and "private-tenant" not in str(paths[0])
    body = paths[0].read_bytes()
    assert hashlib.sha256(body).hexdigest() == reference["artifact_id"]
    assert len(body) == reference["size_bytes"]


@pytest.mark.parametrize("corruption", [b'{"total":99}', b"x" * 100_001], ids=["changed-content", "oversized-content"])
def test_local_artifact_reads_reject_corruption_and_oversized_content(artifacts, corruption):
    reference = artifacts.put("tenant-a", {"total": 42})
    path, = artifacts.root.rglob("*.json")
    path.write_bytes(corruption)
    with pytest.raises(ValueError, match="integrity"):
        artifacts.get("tenant-a", reference["artifact_id"])


def test_rejected_artifact_values_do_not_create_partial_files(artifacts):
    with pytest.raises(ValueError, match="100KB"):
        artifacts.put("tenant-a", {"text": "x" * 100_000})
    with pytest.raises(ValueError):
        artifacts.put("tenant-a", {"total": float("nan")})
    assert not list(artifacts.root.rglob("*"))


def test_failed_atomic_replacement_cleans_temporary_file_and_preserves_committed_value(artifacts, monkeypatch):
    reference = artifacts.put("tenant-a", {"total": 42})
    before = {path: path.read_bytes() for path in artifacts.root.rglob("*") if path.is_file()}

    def disk_failure(source, target):
        assert source != str(target)
        raise OSError("atomic replacement unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("agentic.storage_adapters.os.replace", disk_failure)
        with pytest.raises(OSError, match="replacement unavailable"):
            artifacts.put("tenant-a", {"total": 42})
    after = {path: path.read_bytes() for path in artifacts.root.rglob("*") if path.is_file()}
    assert after == before
    assert artifacts.get("tenant-a", reference["artifact_id"]) == {"total": 42}


def test_secret_files_trim_newlines_and_reject_ambiguous_sources(tmp_path, monkeypatch):
    path = tmp_path / "fixture-secret"
    path.write_text("  fixture-value\n")
    monkeypatch.delenv("LOCAL_TEST_SECRET", raising=False)
    monkeypatch.setenv("LOCAL_TEST_SECRET_FILE", str(path))
    assert secret("LOCAL_TEST_SECRET") == "fixture-value"
    monkeypatch.setenv("LOCAL_TEST_SECRET", "other-value")
    with pytest.raises(ValueError, match="Choose"):
        secret("LOCAL_TEST_SECRET")
    monkeypatch.delenv("LOCAL_TEST_SECRET")
    path.unlink()
    with pytest.raises(FileNotFoundError):
        secret("LOCAL_TEST_SECRET")


def test_rate_limit_rolls_over_and_persists_only_hashed_bucket_identifiers(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REDIS_URL", raising=False)
    clock = [119.99]
    monkeypatch.setattr("agentic.auth.time.time", lambda: clock[0])
    path = str(tmp_path / "limits.db")
    first, second = RateLimiter(path, 2), RateLimiter(path, 2)
    sensitive_bucket = "private-tenant:private-subject"
    assert first.take(sensitive_bucket)
    assert second.take(sensitive_bucket)
    assert not first.take(sensitive_bucket)
    clock[0] = 120.0
    assert second.take(sensitive_bucket)
    assert first.take(sensitive_bucket)
    assert not second.take(sensitive_bucket)
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT bucket,window,requests FROM request_limits").fetchone()
    assert row == (hashlib.sha256(sensitive_bucket.encode()).hexdigest(), 2, 3)
    assert sensitive_bucket not in json.dumps(row)
    clock[0] = 300.0
    assert first.take("new-bucket")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM request_limits").fetchone()[0] == 1


def test_concurrent_rate_limit_cannot_admit_more_than_the_configured_window(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REDIS_URL", raising=False)
    monkeypatch.setattr("agentic.auth.time.time", lambda: 600.0)
    path = str(tmp_path / "limits.db")
    limiters = [RateLimiter(path, 2), RateLimiter(path, 2)]
    ready = threading.Barrier(8)

    def take(index):
        ready.wait(timeout=10)
        return limiters[index % 2].take("shared")

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(take, range(8)))
    assert accepted.count(True) == 2
    assert accepted.count(False) == 6
    assert limiters[0].take("separate")
    for limit in (0, 100_001):
        with pytest.raises(ValueError, match="Rate limit"):
            RateLimiter(path, limit)


@pytest.fixture
def observed_connections(monkeypatch):
    """Retain real connections so garbage collection cannot hide a missing close."""
    original_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.delenv("AGENT_REDIS_URL", raising=False)
    monkeypatch.setattr("agentic.auth.sqlite3.connect", connect)
    try:
        yield original_connect, connections
    finally:
        for connection in connections:
            connection.close()


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_rate_limiter_closes_connections_on_success_and_rolls_back_before_error_cleanup(
    tmp_path, monkeypatch, observed_connections,
):
    original_connect, connections = observed_connections
    clock = [600.0]
    monkeypatch.setattr("agentic.auth.time.time", lambda: clock[0])
    path = str(tmp_path / "limits.db")
    limiter = RateLimiter(path, 2)
    assert_closed(connections[-1])
    assert limiter.take("existing-bucket")
    assert_closed(connections[-1])
    with closing(original_connect(path)) as db, db:
        db.execute("CREATE TRIGGER reject_limit BEFORE INSERT ON request_limits "
                   "BEGIN SELECT RAISE(ABORT, 'quota store rejected write'); END")
    # A new window deletes stale rows before the insert fails. Both operations
    # must roll back together, and the failed connection must still close.
    clock[0] = 900.0
    with pytest.raises(sqlite3.IntegrityError, match="rejected write"):
        limiter.take("new-bucket")
    assert len(connections) == 3
    assert_closed(connections[-1])
    with closing(original_connect(path)) as db:
        rows = db.execute("SELECT bucket,window,requests FROM request_limits").fetchall()
    assert rows == [(hashlib.sha256(b"existing-bucket").hexdigest(), 10, 1)]


def test_rate_limiter_closes_connection_if_schema_initialization_fails(tmp_path, observed_connections):
    _, connections = observed_connections
    path = tmp_path / "invalid.db"
    path.write_text("This is not a SQLite database.")
    with pytest.raises(sqlite3.DatabaseError):
        RateLimiter(str(path), 2)
    assert len(connections) == 1
    assert_closed(connections[0])
