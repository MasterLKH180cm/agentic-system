"""Pool configuration, process ownership and transaction mode contracts."""
import os
from types import SimpleNamespace

import pytest

from agentic import pgpool, postgres

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("name", "value"), [
    ("PG_POOL_SIZE", "bad"), ("PG_POOL_SIZE", "-1"), ("PG_POOL_SIZE", "65"),
    ("PG_POOL_TIMEOUT", "nan"), ("PG_POOL_TIMEOUT", "inf"), ("PG_POOL_TIMEOUT", "0"),
    ("PG_POOL_TIMEOUT", "61"), ("PG_POOL_MAX_WAITING", "0"), ("PG_POOL_MAX_WAITING", "1025"),
])
def test_invalid_pool_limits_fail_before_connecting(monkeypatch, name, value):
    monkeypatch.setenv("AGENT_" + name, value)
    with pytest.raises(ValueError, match="PostgreSQL pool"):
        pgpool.pool_settings()


def test_pool_controls_are_explicit(monkeypatch):
    monkeypatch.setenv("AGENT_PG_POOL_SIZE", "0")
    monkeypatch.setenv("AGENT_PG_POOL_TIMEOUT", "0.5")
    monkeypatch.setenv("AGENT_PG_POOL_MAX_WAITING", "7")
    assert pgpool.pool_settings() == pgpool.PoolSettings(0, 0.5, 7)


def test_inherited_pool_is_never_borrowed_or_closed(monkeypatch):
    closed = []
    pool = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(pgpool, "_pools", {"fixture": pool})
    monkeypatch.setattr(pgpool, "_owner_pid", os.getpid() - 1)
    with pytest.raises(RuntimeError, match="parent process"), pgpool.raw_connection("postgresql://unused"):
        pytest.fail("Inherited connection was borrowed")
    pgpool.close_pools()
    assert not closed


def test_import_before_fork_without_connections_is_allowed(monkeypatch):
    monkeypatch.setattr(pgpool, "_pools", {})
    monkeypatch.setattr(pgpool, "_owner_pid", os.getpid() - 1)
    pgpool._check_process()
    assert pgpool._owner_pid == os.getpid()


def test_owned_pool_cleanup_is_idempotent(monkeypatch):
    closed = []
    monkeypatch.setattr(pgpool, "_owner_pid", os.getpid())
    monkeypatch.setattr(pgpool, "_pools", {"fixture": SimpleNamespace(close=lambda: closed.append(True))})
    pgpool.close_pools()
    pgpool.close_pools()
    assert closed == [True]


def test_readonly_transaction_has_snapshot_without_writer_lock():
    statements = []
    raw = SimpleNamespace(execute=lambda statement: statements.append(statement))
    postgres.Connection(raw, read_only=True)
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert not any("pg_advisory" in statement for statement in statements)
    statements.clear()
    postgres.Connection(raw)
    assert statements[-1] == "SELECT pg_advisory_xact_lock(731824102)"




def test_run_scoped_transaction_shares_the_legacy_compatibility_lock():
    statements = []
    raw = SimpleNamespace(execute=lambda statement: statements.append(statement))
    postgres.Connection(raw, serialized=False)
    assert statements == ["SET LOCAL statement_timeout = '20s'", "SET LOCAL lock_timeout = '20s'",
                          "SELECT pg_advisory_xact_lock_shared(731824102)"]


def test_close_selected_pool_leaves_other_databases_available(monkeypatch):
    closed = []
    first = ("postgresql://first", pgpool.PoolSettings(1, 1, 1))
    second = ("postgresql://second", pgpool.PoolSettings(1, 1, 1))
    monkeypatch.setattr(pgpool, "_pools", {
        first: SimpleNamespace(close=lambda: closed.append("first")),
        second: SimpleNamespace(close=lambda: closed.append("second")),
    })
    pgpool.close_pools("postgresql://first")
    assert closed == ["first"] and list(pgpool._pools) == [second]
    pgpool.close_pools()
    assert closed == ["first", "second"]
