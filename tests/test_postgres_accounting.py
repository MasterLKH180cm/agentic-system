"""Actual isolated PostgreSQL schema migration and 64-bit accounting storage."""
import os
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from agentic.config import ModelRoute
from agentic.model import estimated_cost, reported_usage
from agentic.schemas import Step, WorkflowIn
from agentic.store import SCHEMA, Store

pytestmark = [pytest.mark.integration, pytest.mark.external, pytest.mark.postgres]
ACCOUNTING = ("input_tokens", "output_tokens", "charged_tokens", "cost_microusd")


@pytest.fixture
def database():
    endpoint = os.getenv("AGENT_TEST_POSTGRES_URL")
    if not endpoint:
        pytest.fail("AGENT_TEST_POSTGRES_URL is required for selected PostgreSQL accounting tests")
    import psycopg
    from psycopg import sql

    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"postgres", "postgresql"}:
        pytest.fail("AGENT_TEST_POSTGRES_URL must be a PostgreSQL URL")
    schema = "accounting_test_" + uuid.uuid4().hex
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = (query.get("options", "") + f" -c search_path={schema}").strip()
    isolated = urlunsplit(parsed._replace(query=urlencode(query)))
    with psycopg.connect(endpoint, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(isolated, connect_timeout=5) as connection:
                assert connection.execute("SELECT current_schema()").fetchone()[0] == schema
            yield isolated
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def column_types(store, table):
    with store.connect() as connection:
        return {row["name"]: row["type"] for row in connection.execute(f"PRAGMA table_info({table})")}


def assert_large_accounting(store):
    workflow = WorkflowIn(goal="Accounting boundary", steps=[
        Step(id="calculate", tool="math.calculate", args={"expression": "2+3"}),
    ])
    run_id = store.submit("tenant", workflow)["id"]
    route = ModelRoute("wide-cost", "anthropic", "fixture", "https://provider.invalid", "fixture-key",
                       input_cost_per_million=1_000_000, output_cost_per_million=1_000_000)
    reserved_cost = estimated_cost(route, 11_000, 1_000)
    assert reserved_cost == 12_000_000_000
    reservation = store.reserve_usage("tenant", run_id, route.name, "synthesis", 12_000, reserved_cost)
    reserved, = store.usage("tenant", run_id)["calls"]
    assert reserved["status"] == "reserved" and reserved["cost_microusd"] == reserved_cost
    counts = reported_usage("anthropic", {
        "input_tokens": 2_000_000_000, "output_tokens": 2_000_000_000,
        "cache_read_input_tokens": 2_000_000_000, "cache_creation_input_tokens": 2_000_000_000,
    })
    assert counts == (6_000_000_000, 2_000_000_000)
    actual_cost = estimated_cost(route, *counts)
    store.settle_usage(reservation, input_tokens=counts[0], output_tokens=counts[1], cost_microusd=actual_cost)
    settled, = store.usage("tenant", run_id)["calls"]
    assert (settled["input_tokens"], settled["output_tokens"], settled["charged_tokens"]) == (*counts, 8_000_000_000)
    assert settled["cost_microusd"] == 8_000_000_000_000_000
    assert settled["status"] == "reported"
    assert len([event for event in store.events("tenant", run_id) if event["type"] == "model_usage"]) == 1
    return run_id, settled


def test_fresh_schema_supports_large_valid_reservations_and_reported_charges(database):
    store = Store(database)
    types = column_types(store, "model_usage")
    assert all(types[column] == "bigint" for column in ACCOUNTING)
    assert column_types(store, "steps")["attempts"] == "integer"
    run_id, settled = assert_large_accounting(store)
    reopened = Store(database)
    assert reopened.usage("tenant", run_id)["calls"] == [settled]
    assert column_types(reopened, "model_usage") == types


def test_existing_integer_columns_migrate_without_losing_records_and_repeat_safely(database):
    import psycopg
    from psycopg.rows import dict_row

    # Build the prior PostgreSQL schema, with the four accounting INTEGER fields.
    legacy = re.sub(r"\bREAL\b", "DOUBLE PRECISION",
                    SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"))
    with psycopg.connect(database, row_factory=dict_row, connect_timeout=5) as connection:
        for statement in legacy.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO runs(id,tenant,goal,status,request_hash,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("legacy-run", "legacy-tenant", "Existing workflow", "queued", "fixture-hash",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO steps(run_id,id,tool,args,depends_on,status) VALUES(%s,%s,%s,%s,%s,%s)",
            ("legacy-run", "calculate", "math.calculate", '{"expression":"2+3"}', "[]", "pending"),
        )
        connection.execute(
            "INSERT INTO model_usage(id,tenant,run_id,route,purpose,input_tokens,output_tokens,charged_tokens,"
            "cost_microusd,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("legacy-call", "legacy-tenant", "legacy-run", "legacy-route", "synthesis", 2, 3, 5, 17, "reported",
             "2024-01-01T00:00:00+00:00"),
        )
        before = connection.execute("SELECT * FROM model_usage WHERE id=%s", ("legacy-call",)).fetchone()
        run_before = connection.execute("SELECT * FROM runs WHERE id=%s", ("legacy-run",)).fetchone()
        step_before = connection.execute("SELECT * FROM steps WHERE run_id=%s", ("legacy-run",)).fetchone()
        type_rows = connection.execute(
            "SELECT column_name,data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='model_usage'"
        ).fetchall()
        types_before = {row["column_name"]: row["data_type"] for row in type_rows}
        assert all(types_before[column] == "integer" for column in ACCOUNTING)
    store = Store(database)
    assert store.usage("legacy-tenant", "legacy-run")["calls"] == [before]
    with store.connect() as connection:
        assert dict(connection.execute("SELECT * FROM runs WHERE id=?", ("legacy-run",)).fetchone()) == run_before
        assert dict(connection.execute("SELECT * FROM steps WHERE run_id=?", ("legacy-run",)).fetchone()) == step_before
    types = column_types(store, "model_usage")
    assert all(types[column] == "bigint" for column in ACCOUNTING)
    assert column_types(store, "steps")["attempts"] == "integer"
    assert column_types(store, "definitions")["version"] == "integer"
    run_id, settled = assert_large_accounting(store)
    repeated = Store(database)
    assert repeated.usage("legacy-tenant", "legacy-run")["calls"] == [before]
    assert repeated.usage("tenant", run_id)["calls"] == [settled]
    assert column_types(repeated, "model_usage") == types
