"""Pure PostgreSQL schema and parameter contracts; no database service required."""
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentic import postgres
from agentic.store import SCHEMA, BudgetExceeded, Store

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


def test_reservation_aggregates_have_distinct_mapping_labels_and_enforce_both_budgets(monkeypatch):
    """Model PostgreSQL's duplicate default labels without needing its service."""
    charges = [(40, 60)]

    class Connection:
        def execute(self, statement, parameters=()):
            row = None
            if statement.startswith("SELECT specification,status"):
                row = {"specification": json.dumps({"max_model_tokens": 100, "max_cost_usd": 0.0001}),
                       "status": "running"}
            elif statement.startswith("SELECT COALESCE(SUM("):
                # PostgreSQL names both unaliased expressions `coalesce`; dict_row
                # would retain only the last value. Derive labels from the actual SQL.
                aliases = re.findall(r"COALESCE\(SUM\([a-z_]+\),0\)(?:\s+AS\s+([a-z_]+))?",
                                     statement, flags=re.IGNORECASE)
                labels = [alias or "coalesce" for alias in aliases]
                assert len(labels) == 2 and len(set(labels)) == 2, "Accounting result labels must be unique"
                row = dict(zip(labels, (sum(tokens for tokens, _ in charges), sum(cost for _, cost in charges)), strict=True))
            elif statement.startswith("INSERT INTO model_usage"):
                charges.append((parameters[5], parameters[6]))
            elif statement != "BEGIN IMMEDIATE" and not statement.startswith("INSERT INTO events"):
                pytest.fail("Unexpected storage statement")
            # A mapping-only row also prevents renewed dependence on column positions.
            return SimpleNamespace(fetchone=lambda: row)

    @contextmanager
    def connect():
        yield Connection()

    store = object.__new__(Store)
    monkeypatch.setattr(store, "connect", connect)
    assert store.reserve_usage("tenant", "run", "route", "synthesis", 50, 30)
    assert charges == [(40, 60), (50, 30)]
    with pytest.raises(BudgetExceeded, match="Token reservation"):
        store.reserve_usage("tenant", "run", "route", "synthesis", 11, 1)
    with pytest.raises(BudgetExceeded, match="Cost reservation"):
        store.reserve_usage("tenant", "run", "route", "synthesis", 1, 11)
    assert charges == [(40, 60), (50, 30)]
