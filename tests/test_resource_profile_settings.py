from __future__ import annotations

import sqlite3
import threading
from dataclasses import fields
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.resource_profiles import (
    DESKTOP_RESOURCE_OWNER,
    DESKTOP_RESOURCE_SCOPE,
    ResourceProfileConflict,
    ResourceProfileName,
    ResourceProfileSettings,
)
from nika_core.resource_profiles.settings import MAX_PROFILE_REVISION
from nika_core.resources.contracts import ResourceBudget, ResourceSnapshot
from nika_core.resources.manager import ResourceManager

_GIB = 1024**3
_IDENTITY = {"scope": DESKTOP_RESOURCE_SCOPE, "owner_id": DESKTOP_RESOURCE_OWNER}


class Observer:
    def __init__(self, *, cpu: float = 10.0, available: int = 8 * _GIB) -> None:
        self.current = ResourceSnapshot(cpu, 20.0, available)
        self.calls = 0

    def snapshot(self) -> ResourceSnapshot:
        self.calls += 1
        return self.current


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ресурсні профілі зі пробілами" / "стан.sqlite3")
    store.initialize()
    return store


def _manager(store: SQLiteStore, observer: Observer, settings: ResourceProfileSettings):
    return ResourceManager(store, observer, admission_policy=settings)


def test_selection_persists_without_default_overwrite_and_budget_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    assert settings.snapshot() == {"profile": "normal", "revision": 1}
    current = settings.configure(profile=ResourceProfileName.ECONOMY, expected_revision=1)
    reopened = ResourceProfileSettings(
        SQLiteStore(store.path),
        default_profile=ResourceProfileName.LOW_MEMORY,
    )
    assert reopened.selection() == current
    manager = _manager(store, Observer(), reopened)
    budget = manager.get_budget(**_IDENTITY)
    assert (budget.max_cpu_percent, budget.max_memory_percent, budget.max_concurrent) == (75, 80, 1)
    events = AuditLog(store).list_for(entity_type="resource_pool", entity_id=DESKTOP_RESOURCE_OWNER)
    assert len(events) == 1
    assert events[0].payload == {
        "profile": "economy",
        "previous_profile": "normal",
        "revision": 2,
    }
    assert len(fields(ResourceBudget)) == 5
    assert len(fields(ResourceSnapshot)) == 8


@pytest.mark.parametrize("profile", list(ResourceProfileName))
def test_actual_manager_enforces_each_selected_available_memory_floor(
    tmp_path: Path,
    profile: ResourceProfileName,
) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store, default_profile=profile)
    floor = {
        ResourceProfileName.NORMAL: _GIB // 2,
        ResourceProfileName.ECONOMY: _GIB,
        ResourceProfileName.NIGHT_BATCH: _GIB,
        ResourceProfileName.LOW_MEMORY: 2 * _GIB,
    }[profile]
    observer = Observer(available=floor - 1)
    manager = _manager(store, observer, settings)
    denied = manager.request(**_IDENTITY, request_id="очікування")
    assert (denied.granted, denied.reason) == (False, "available_memory_floor")
    assert manager.active_count(**_IDENTITY) == 0
    assert manager.queued(**_IDENTITY) == ("очікування",)
    assert manager.status(**_IDENTITY).pressure_reasons == ("available_memory_floor",)
    observer.current = ResourceSnapshot(10, 20, floor)
    granted = manager.request(**_IDENTITY, request_id="очікування")
    assert granted.granted is True
    assert manager.revalidate(**_IDENTITY, request_id="очікування").granted is True
    assert manager.release(**_IDENTITY, request_id="очікування") is True


def test_profile_change_revalidates_existing_grant_without_releasing_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    observer = Observer(cpu=80, available=_GIB)
    manager = _manager(store, observer, settings)
    assert manager.request(**_IDENTITY, request_id="running").granted is True
    settings.configure(profile=ResourceProfileName.ECONOMY, expected_revision=1)
    denied = manager.revalidate(**_IDENTITY, request_id="running")
    assert (denied.granted, denied.reason) == (False, "cpu_limit")
    assert manager.active_count(**_IDENTITY) == 1
    assert "cpu_limit" in manager.status(**_IDENTITY).pressure_reasons
    assert manager.release(**_IDENTITY, request_id="running") is True


def test_profile_selection_and_grant_read_share_the_same_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    calls: list[tuple[bool, int, str]] = []

    class Policy:
        def denial_reason(self, conn, **values):
            calls.append((conn.in_transaction, values["active_count"], values["scope"]))
            assert (
                conn.execute("SELECT profile FROM resource_profile_selection").fetchone()[0]
                == "normal"
            )
            return settings.denial_reason(conn, **values)

    observer = Observer()
    manager = ResourceManager(store, observer, admission_policy=Policy())
    assert manager.request(**_IDENTITY, request_id="one").granted is True
    assert calls == [(True, 0, DESKTOP_RESOURCE_SCOPE)]
    assert observer.calls == 1
    assert manager.revalidate(**_IDENTITY, request_id="one").granted is True
    assert calls[-1] == (True, 1, DESKTOP_RESOURCE_SCOPE)
    assert observer.calls == 2


def test_independent_settings_editors_have_one_revision_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = ResourceProfileSettings(store)
    second = ResourceProfileSettings(SQLiteStore(store.path))
    barrier = threading.Barrier(2)
    winners: list[ResourceProfileName] = []
    conflicts: list[ResourceProfileConflict] = []
    errors: list[Exception] = []

    def edit(settings, profile):
        try:
            barrier.wait(timeout=5)
            winners.append(settings.configure(profile=profile, expected_revision=1).profile)
        except ResourceProfileConflict as error:
            conflicts.append(error)
        except Exception as error:  # noqa: BLE001 - collect unexpected thread failures for assertion
            errors.append(error)

    threads = [
        threading.Thread(target=edit, args=(first, ResourceProfileName.ECONOMY)),
        threading.Thread(target=edit, args=(second, ResourceProfileName.LOW_MEMORY)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert len(winners) == len(conflicts) == 1
    assert first.selection().profile is winners[0]
    assert first.selection().revision == 2
    assert (
        len(
            AuditLog(store).list_for(
                entity_type="resource_pool",
                entity_id=DESKTOP_RESOURCE_OWNER,
            )
        )
        == 1
    )


def test_audit_failure_rolls_back_selection_budget_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    manager = _manager(store, Observer(), settings)
    before = manager.get_budget(**_IDENTITY)

    def fail(*_args, **_kwargs):
        raise OSError("injected audit failure")

    monkeypatch.setattr(settings._audit, "append_with_connection", fail)
    with pytest.raises(OSError, match="audit failure"):
        settings.configure(profile=ResourceProfileName.ECONOMY, expected_revision=1)
    assert settings.snapshot() == {"profile": "normal", "revision": 1}
    assert manager.get_budget(**_IDENTITY) == before


@pytest.mark.parametrize("version", [0, -1, 2, 100])
def test_invalid_or_newer_ordered_migration_history_is_rejected(
    tmp_path: Path,
    version: int,
) -> None:
    store = _store(tmp_path)
    ResourceProfileSettings(store)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO resource_profile_schema_migrations VALUES (?, 'invalid')",
            (version,),
        )
    with pytest.raises(RuntimeError, match="migration history"):
        ResourceProfileSettings(store)


@pytest.mark.parametrize(
    "table", ["resource_profile_schema_migrations", "resource_profile_selection"]
)
def test_missing_owned_table_is_not_silently_recreated(tmp_path: Path, table: str) -> None:
    store = _store(tmp_path)
    ResourceProfileSettings(store)
    with store.connection() as conn:
        conn.execute(f"DROP TABLE {table}")
    with pytest.raises(RuntimeError, match="incomplete"):
        ResourceProfileSettings(store)


def test_corrupt_or_newer_profile_state_never_grants(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    with store.connection() as conn:
        conn.execute("DELETE FROM resource_profile_schema_migrations")
    manager = _manager(store, Observer(), settings)
    result = manager.request(**_IDENTITY, request_id="no-effect")
    assert (result.granted, result.reason) == (False, "profile_policy_unavailable")
    assert manager.active_count(**_IDENTITY) == 0


@pytest.mark.parametrize("profile", ["normal", True, 1, None])
def test_configuration_requires_exact_profile_enum(tmp_path: Path, profile: object) -> None:
    settings = ResourceProfileSettings(_store(tmp_path))
    with pytest.raises(ValueError, match="exact ResourceProfileName"):
        settings.configure(profile=profile, expected_revision=1)


@pytest.mark.parametrize("revision", [True, 1.0, "1", 0, -1, MAX_PROFILE_REVISION + 1])
def test_configuration_requires_exact_revision(tmp_path: Path, revision: object) -> None:
    settings = ResourceProfileSettings(_store(tmp_path))
    with pytest.raises(ValueError, match="exact supported integer"):
        settings.configure(profile=ResourceProfileName.ECONOMY, expected_revision=revision)


def test_other_resource_scopes_are_not_affected_by_desktop_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store, default_profile=ResourceProfileName.LOW_MEMORY)
    manager = _manager(store, Observer(available=0), settings)
    assert manager.request(scope="media", owner_id="own", request_id="separate").granted is True


@pytest.mark.parametrize("value", [False, 1, "", "contains space", "x" * 129])
def test_invalid_policy_output_cannot_grant(tmp_path: Path, value: object) -> None:
    store = _store(tmp_path)

    class Policy:
        def denial_reason(self, _conn, **_values):
            return value

    manager = ResourceManager(store, Observer(), admission_policy=Policy())
    decision = manager.request(**_IDENTITY, request_id="bad-policy")
    assert (decision.granted, decision.reason) == (False, "invalid_profile_policy")
    assert manager.active_count(**_IDENTITY) == 0


def test_policy_failure_is_non_granting_and_does_not_expose_diagnostics(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class Policy:
        def denial_reason(self, _conn, **_values):
            raise OSError("private process diagnostic must not escape")

    manager = ResourceManager(store, Observer(), admission_policy=Policy())
    result = manager.request(**_IDENTITY, request_id="failed-policy")
    assert result.reason == "profile_policy_unavailable"
    assert result.granted is False
    assert manager.active_count(**_IDENTITY) == 0


def test_policy_uses_detached_validated_snapshot_not_retained_observer_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = ResourceProfileSettings(store)
    observer = Observer(available=0)

    class Policy:
        def denial_reason(self, conn, **values):
            object.__setattr__(observer.current, "available_memory_bytes", 8 * _GIB)
            return settings.denial_reason(conn, **values)

    manager = ResourceManager(store, observer, admission_policy=Policy())
    denied = manager.request(**_IDENTITY, request_id="retained-mutation")
    assert (denied.granted, denied.reason) == (False, "available_memory_floor")
    assert observer.calls == 1
    assert manager.active_count(**_IDENTITY) == 0


def test_budget_writer_does_not_commit_caller_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ResourceProfileSettings(store)
    with pytest.raises(sqlite3.OperationalError), store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ResourceManager.set_budget_with_connection(
            conn, ResourceBudget(**_IDENTITY, max_concurrent=7)
        )
        conn.execute("SELECT nonexistent_column FROM resource_profile_selection")
    assert ResourceManager(store, Observer()).get_budget(**_IDENTITY).max_concurrent == 1


def test_invalid_budget_keeps_preconnection_validation_and_creates_no_database(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "must-not-exist" / "state.sqlite3")
    manager = ResourceManager(store, Observer())
    with pytest.raises(ValueError, match="scope and owner_id"):
        manager.set_budget(ResourceBudget(scope="", owner_id="invalid"))
    assert not store.path.exists()
    assert not store.path.parent.exists()
