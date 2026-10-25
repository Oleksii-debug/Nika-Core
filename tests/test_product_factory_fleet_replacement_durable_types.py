from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pf3_fleet_replacement_support import _authorized_plan, _fixture, _submit

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_fleet_replacement import FleetReplacementError
from nika_core.product_factory_fleet_replacement_durability import (
    SQLiteFleetReplacementDispatchJournal,
)


def _pending_dispatch(tmp_path):
    path = tmp_path / "fleet-durable-types.db"
    store = SQLiteStore(path)
    journal = SQLiteFleetReplacementDispatchJournal(store)
    coordinator, _, _, _, port, placements = _fixture(
        service_count=1,
        dispatch_journal=journal,
    )
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)
    port.modes[request_id] = "hard-crash"
    with pytest.raises(SystemExit):
        coordinator.advance(
            plan.plan_id,
            now=datetime(2026, 8, 23, 12, tzinfo=UTC),
        )
    return store, journal, plan.plan_id, request_id


def _terminal_dispatch(tmp_path):
    path = tmp_path / "fleet-terminal-durable-types.db"
    store = SQLiteStore(path)
    journal = SQLiteFleetReplacementDispatchJournal(store)
    coordinator, _, _, _, _, placements = _fixture(
        service_count=1,
        dispatch_journal=journal,
    )
    key = ("service-000", "service-000-replica-0")
    plan, authority, review_ref = _authorized_plan(placements, (key,))
    request_id = f"{plan.plan_id}:service-000:service-000-replica-0:replace:1"
    _submit(coordinator, plan, authority, review_ref)
    coordinator.advance(
        plan.plan_id,
        now=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )
    return store, plan.plan_id, request_id


@pytest.mark.parametrize("version", [1.5, "not-an-integer"])
def test_schema_version_rejects_non_integer_storage_type(tmp_path, version) -> None:
    store = SQLiteStore(tmp_path / "fleet-schema-types.db")
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE fleet_replacement_schema_migrations(version, applied_at)"
        )
        conn.execute(
            "INSERT INTO fleet_replacement_schema_migrations VALUES (?, ?)",
            (version, "2026-08-24T07:30:00+00:00"),
        )

    with pytest.raises(FleetReplacementError, match="schema version storage type"):
        SQLiteFleetReplacementDispatchJournal(store)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("attempt", 1.5),
        ("attempt", "not-an-integer"),
        ("source_was_enabled", 0.5),
        ("source_was_enabled", "not-an-integer"),
    ],
)
def test_dispatch_numeric_authority_rejects_sqlite_coercion(tmp_path, column, value) -> None:
    store, _, plan_id, request_id = _pending_dispatch(tmp_path)
    assert column in {"attempt", "source_was_enabled"}
    with store.connection() as conn:
        conn.execute(
            f"UPDATE fleet_replacement_dispatches SET {column} = ? WHERE request_id = ?",
            (value, request_id),
        )

    restarted = SQLiteFleetReplacementDispatchJournal(SQLiteStore(store.path))
    with pytest.raises(FleetReplacementError, match="storage type must be INTEGER"):
        restarted.list_plan(plan_id)


@pytest.mark.parametrize(
    "column",
    ["request_json", "request_checksum_sha256", "created_at"],
)
def test_dispatch_text_authority_rejects_blob_storage(tmp_path, column) -> None:
    store, _, plan_id, request_id = _pending_dispatch(tmp_path)
    assert column in {"request_json", "request_checksum_sha256", "created_at"}
    with store.connection() as conn:
        conn.execute(
            f"""
            UPDATE fleet_replacement_dispatches
            SET {column} = CAST({column} AS BLOB)
            WHERE request_id = ?
            """,
            (request_id,),
        )

    restarted = SQLiteFleetReplacementDispatchJournal(SQLiteStore(store.path))
    with pytest.raises(FleetReplacementError, match="storage type must be TEXT"):
        restarted.list_plan(plan_id)


@pytest.mark.parametrize(
    "column",
    ["result_json", "result_checksum_sha256", "resolved_at"],
)
def test_terminal_text_authority_rejects_blob_storage(tmp_path, column) -> None:
    store, plan_id, request_id = _terminal_dispatch(tmp_path)
    assert column in {"result_json", "result_checksum_sha256", "resolved_at"}
    with store.connection() as conn:
        conn.execute(
            f"""
            UPDATE fleet_replacement_dispatches
            SET {column} = CAST({column} AS BLOB)
            WHERE request_id = ?
            """,
            (request_id,),
        )

    restarted = SQLiteFleetReplacementDispatchJournal(SQLiteStore(store.path))
    with pytest.raises(FleetReplacementError, match="storage type must be TEXT"):
        restarted.list_plan(plan_id)


def test_prepare_rejects_boolean_attempt_identity(tmp_path) -> None:
    _, journal, plan_id, _ = _pending_dispatch(tmp_path)
    durable = journal.list_plan(plan_id)[0]

    with pytest.raises(FleetReplacementError, match="attempt must be positive"):
        journal.prepare(
            durable.request,
            attempt=True,
            source_was_enabled=durable.source_was_enabled,
        )
