"""Pure PostgreSQL schema and parameter contracts; no database service required."""
import re
from types import SimpleNamespace

import pytest

from agentic import postgres
from agentic.store import SCHEMA

pytestmark = pytest.mark.unit
ACCOUNTING = ("input_tokens", "output_tokens", "charged_tokens", "cost_microusd")


def test_fresh_postgres_schema_widens_only_model_usage_accounting_columns():
    source = """
        CREATE TABLE other_counts (input_tokens INTEGER, charged_tokens INTEGER);
        CREATE TABLE IF NOT EXISTS model_usage (
            input_tokens INTEGER, output_tokens INTEGER, charged_tokens INTEGER NOT NULL,
            cost_microusd INTEGER, attempts INTEGER, note TEXT DEFAULT 'input_tokens INTEGER'
        );
    """
    translated = postgres.translate_schema(source)
    assert "other_counts (input_tokens INTEGER, charged_tokens INTEGER)" in translated
    for column in ACCOUNTING:
        assert re.search(rf"\b{column} BIGINT\b", translated)
    assert "attempts INTEGER" in translated
    assert "DEFAULT 'input_tokens INTEGER'" in translated
    assert postgres.translate_schema(translated) == translated
    production = postgres.translate_schema(SCHEMA)
    assert "id BIGSERIAL PRIMARY KEY" in production
    assert "deadline_at DOUBLE PRECISION" in production
    assert "attempts INTEGER" in production and "version INTEGER" in production
    assert "charged_tokens INTEGER NOT NULL" in SCHEMA  # SQLite's schema remains unchanged.


@pytest.mark.parametrize("already_wide", [(), ("output_tokens", "cost_microusd"), ACCOUNTING],
                         ids=["legacy", "partial-migration", "already-migrated"])
def test_accounting_migration_is_idempotent_and_leaves_unrelated_columns_alone(already_wide):
    types = {column: "bigint" if column in already_wide else "integer" for column in ACCOUNTING}
    types.update(attempts="integer", route="text")
    statements = []

    class Connection:
        def execute(self, statement):
            statements.append(statement)
            if statement == "PRAGMA table_info(model_usage)":
                return [{"name": name, "type": kind} for name, kind in types.items()]
            altered = re.findall(r"ALTER COLUMN ([a-z_]+) TYPE BIGINT", statement)
            assert statement.startswith("ALTER TABLE model_usage ")
            assert set(altered) == set(ACCOUNTING) - set(already_wide)
            for column in altered:
                types[column] = "bigint"

    connection = Connection()
    postgres.migrate_accounting(connection)
    postgres.migrate_accounting(connection)
    assert all(types[column] == "bigint" for column in ACCOUNTING)
    assert types["attempts"] == "integer" and types["route"] == "text"
    alterations = [statement for statement in statements if statement.startswith("ALTER TABLE")]
    assert len(alterations) == (0 if set(already_wide) == set(ACCOUNTING) else 1)


def test_schema_and_placeholder_translation_never_rewrites_bound_accounting_values():
    calls = []

    def execute(sql, values):
        calls.append((sql, values))
        return SimpleNamespace(rowcount=1)

    connection = object.__new__(postgres.Connection)
    connection.raw = SimpleNamespace(execute=execute)
    values = ("input_tokens INTEGER; REAL ?", 12_000_000_000)
    cursor = connection.execute("INSERT INTO model_usage(route,cost_microusd) VALUES(?,?)", values)
    assert cursor.rowcount == 1
    assert calls == [("INSERT INTO model_usage(route,cost_microusd) VALUES(%s,%s)", values)]
    assert calls[0][1] is values
