"""PostgreSQL vocabulary adapter with pooled, explicitly scoped transactions.

Control-plane writes retain an exclusive advisory lock. Workflow transitions
share its compatibility lock and lock individual run rows; claims skip locked
runs. Read-only transactions use a
repeatable snapshot and do not acquire either write lock.
"""
import re
import sqlite3
from contextlib import contextmanager

from agentic.pgpool import raw_connection

ACCOUNTING_COLUMNS = ("input_tokens", "output_tokens", "charged_tokens", "cost_microusd")


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
    schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)

    def widen_accounting(match):
        # Only this known table's declarations change; quoted SQL literals stay intact.
        fragments = re.split(r"('(?:''|[^'])*')", match.group())
        for index in range(0, len(fragments), 2):
            for column in ACCOUNTING_COLUMNS:
                fragments[index] = re.sub(rf"(\b{column}\s+)INTEGER\b", r"\1BIGINT", fragments[index], flags=re.IGNORECASE)
        return "".join(fragments)

    return re.sub(r"^[ \t]*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?model_usage\s*\([^;]+\)",
                  widen_accounting, schema, flags=re.IGNORECASE | re.MULTILINE)


def migrate_accounting(connection) -> None:
    """Widen legacy accounting in the caller's serialized initialization transaction."""
    types = {row["name"]: row["type"] for row in connection.execute("PRAGMA table_info(model_usage)")}
    narrow = [column for column in ACCOUNTING_COLUMNS if types.get(column) == "integer"]
    if narrow:
        # One ALTER rewrites the table once and preserves all records/constraints.
        connection.execute("ALTER TABLE model_usage " + ", ".join(
            f"ALTER COLUMN {column} TYPE BIGINT" for column in narrow
        ))


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
    def __init__(self, raw, *, read_only: bool = False, serialized: bool = True):
        self.raw = raw
        if read_only:
            raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        raw.execute("SET LOCAL statement_timeout = '20s'")
        raw.execute("SET LOCAL lock_timeout = '20s'")
        if not read_only:
            # Legacy writers hold the exclusive form of the same key. New run
            # writers share this bridge, so they remain concurrent while never
            # racing a pre-upgrade writer or a schema/admission transaction.
            raw.execute("SELECT pg_advisory_xact_lock(731824102)" if serialized else
                        "SELECT pg_advisory_xact_lock_shared(731824102)")

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
def connect(dsn: str, *, read_only: bool = False, serialized: bool = True):
    with raw_connection(dsn) as raw:
        yield Connection(raw, read_only=read_only, serialized=serialized)
