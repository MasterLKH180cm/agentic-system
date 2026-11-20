"""PostgreSQL adapter for the runtime's deliberately small SQL vocabulary.

Transactions use a database-local advisory lock to preserve the existing
serialized queue semantics across hosts. Tool execution happens outside it.
This is a correctness-first backend, not a high-throughput SKIP LOCKED queue.
"""
from contextlib import contextmanager
import re
import sqlite3


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return None if row is None else Row(row)

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield Row(row)


def translate_schema(schema: str) -> str:
    schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    # Epoch seconds cannot be stored in PostgreSQL's 32-bit REAL without losing lease precision.
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)


def bind_parameters(sql: str) -> str:
    """Translate only placeholder markers outside SQL string literals."""
    result, quoted, index = [], False, 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            if quoted and index + 1 < len(sql) and sql[index + 1] == "'":
                result.append("''")
                index += 2
                continue
            quoted = not quoted
        result.append("%s" if char == "?" and not quoted else char)
        index += 1
    return "".join(result)


class Connection:
    def __init__(self, raw):
        self.raw = raw
        raw.execute("SET LOCAL statement_timeout = '20s'")
        raw.execute("SET LOCAL lock_timeout = '20s'")
        raw.execute("SELECT pg_advisory_xact_lock(731824102)")

    def execute(self, statement: str, parameters=()):
        sql = statement.strip()
        if sql == "BEGIN IMMEDIATE":
            return Cursor(self.raw.execute("SELECT 1"))
        info = re.fullmatch(r"PRAGMA table_info\(([a-z_]+)\)", sql)
        if info:
            return Cursor(self.raw.execute(
                "SELECT ordinal_position-1 AS cid, column_name AS name, data_type AS type "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s ORDER BY ordinal_position", (info.group(1),),
            ))
        if sql.startswith("PRAGMA"):
            raise ValueError("SQLite pragma is not supported by the PostgreSQL backend")
        if "rowid" in sql.lower():
            raise ValueError("PostgreSQL migrations require explicit ordinals")
        # SQL is internal static application code; values always remain bound parameters.
        sql = bind_parameters(translate_schema(sql))
        try:
            return Cursor(self.raw.execute(sql, parameters))
        except Exception as exc:
            import psycopg
            if isinstance(exc, psycopg.IntegrityError):
                raise sqlite3.IntegrityError("Persistent constraint violation") from exc
            raise

    def executescript(self, script: str):
        for statement in translate_schema(script).split(";"):
            if statement.strip():
                self.execute(statement)


@contextmanager
def connect(dsn: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ValueError("Install the postgres extra for AGENT_DATABASE_URL") from exc
    raw = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    try:
        yield Connection(raw)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()
