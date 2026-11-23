"""Bounded PostgreSQL pools owned by the process that first opens them.

Workers must use spawn/exec, or open their connections after a fork. Reusing an
already live pool in a forked child fails before any inherited lock or socket is
touched. All transaction settings are LOCAL and reset on commit/rollback.
"""
import atexit
import math
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass

PREFIX = "AGENT"


@dataclass(frozen=True)
class PoolSettings:
    size: int
    timeout: float
    max_waiting: int


def pool_settings() -> PoolSettings:
    try:
        size = int(os.getenv(PREFIX + "_PG_POOL_SIZE", "4"))
        timeout = float(os.getenv(PREFIX + "_PG_POOL_TIMEOUT", "5"))
        waiting = int(os.getenv(PREFIX + "_PG_POOL_MAX_WAITING", "32"))
    except ValueError:
        raise ValueError(PREFIX + " PostgreSQL pool settings must be numeric") from None
    if not 0 <= size <= 64 or not math.isfinite(timeout) or not 0.1 <= timeout <= 60 or not 1 <= waiting <= 1024:
        raise ValueError(PREFIX + " PostgreSQL pool requires size 0..64, timeout 0.1..60, waiting 1..1024")
    return PoolSettings(size, timeout, waiting)


_pools = {}
_owner_pid = os.getpid()
_lock = threading.Lock()


def _check_process():
    global _owner_pid, _lock
    current = os.getpid()
    if current != _owner_pid:
        if _pools:
            raise RuntimeError("PostgreSQL pool belongs to the parent process; use spawn/exec or open connections after fork")
        _owner_pid = current
        _lock = threading.Lock()


def close_pools(dsn: str | None = None) -> None:
    """Close only this process's pools; callers must first stop their workers."""
    if os.getpid() != _owner_pid:
        return
    with _lock:
        keys = [key for key in _pools if dsn is None or key[0] == dsn]
        pools = [_pools.pop(key) for key in keys]
    for pool in pools:
        pool.close()


@contextmanager
def raw_connection(dsn: str):
    settings = pool_settings()
    _check_process()
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ValueError("Install the postgres extra for " + PREFIX + "_DATABASE_URL") from exc
    if settings.size:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ValueError("Install the postgres extra including psycopg_pool, or set " + PREFIX + "_PG_POOL_SIZE=0") from exc
        key = (dsn, settings)
        with _lock:
            pool = _pools.get(key)
            if pool is None:
                pool = ConnectionPool(dsn, min_size=0, max_size=settings.size, timeout=settings.timeout,
                                      max_waiting=settings.max_waiting, max_idle=60, reconnect_timeout=5,
                                      kwargs={"row_factory": dict_row, "connect_timeout": 5},
                                      check=ConnectionPool.check_connection, open=True,
                                      name=PREFIX.lower() + "-postgres")
                _pools[key] = pool
        with pool.connection() as raw:
            yield raw
    else:
        raw = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
        try:
            yield raw
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


atexit.register(close_pools)

