"""Persisted desktop profile selection over canonical SQLite budgets and grants."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.resource_profiles.adapter import budget_for_profile
from nika_core.resource_profiles.contracts import ResourceProfileName, WorkloadClass
from nika_core.resource_profiles.policy import ResourceProfilePolicy
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager

DESKTOP_RESOURCE_SCOPE = "desktop_runtime"
DESKTOP_RESOURCE_OWNER = "local_machine"
MAX_PROFILE_REVISION = (1 << 53) - 1
_MARKER = "resource_profile_schema_migrations"
_SELECTION = "resource_profile_selection"
_SELECTION_SQL = (
    f"CREATE TABLE {_SELECTION} ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
    "profile TEXT NOT NULL CHECK(profile IN ('normal', 'economy', 'night_batch', 'low_memory')), "
    f"revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND {MAX_PROFILE_REVISION}))"
)


class ResourceProfileConflict(ValueError):
    """A settings editor attempted to overwrite a newer committed selection."""


@dataclass(frozen=True, slots=True)
class ResourceProfileSelection:
    profile: ResourceProfileName
    revision: int


class ResourceProfileSettings:
    """Desktop-only profile authority, also the manager's trusted admission port.

    No workload class comes from the UI. Ordinary desktop work is GENERAL; model,
    browser and transcription composition retain their own owners and authority.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        default_profile: ResourceProfileName = ResourceProfileName.NORMAL,
    ) -> None:
        _require_profile(default_profile)
        self._store = store
        self._audit = AuditLog(store)
        self._policy = ResourceProfilePolicy()
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = _table_exists(conn, _MARKER)
            selection = _table_exists(conn, _SELECTION)
            if not marker and not selection:
                conn.execute(
                    f"CREATE TABLE {_MARKER} ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                conn.execute(_SELECTION_SQL)
                conn.execute(
                    f"INSERT INTO {_MARKER}(version, applied_at) VALUES (1, ?)",
                    (datetime.now(UTC).isoformat(),),
                )
                conn.execute(
                    f"INSERT INTO {_SELECTION}(singleton, profile, revision) VALUES (1, ?, 1)",
                    (default_profile.value,),
                )
            current = self._read_with_connection(conn)
            self._write_budget(conn, current.profile)

    def selection(self) -> ResourceProfileSelection:
        with self._store.connection() as conn:
            conn.execute("BEGIN")
            return self._read_with_connection(conn)

    def snapshot(self) -> dict[str, str | int]:
        current = self.selection()
        return {"profile": current.profile.value, "revision": current.revision}

    def configure(
        self,
        *,
        profile: ResourceProfileName,
        expected_revision: int,
    ) -> ResourceProfileSelection:
        _require_profile(profile)
        if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_PROFILE_REVISION:
            raise ValueError("resource profile revision must be an exact supported integer")
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._read_with_connection(conn)
            if current.revision != expected_revision:
                raise ResourceProfileConflict(
                    "resource profile settings changed; refresh and retry"
                )
            if profile is current.profile:
                return current
            if current.revision == MAX_PROFILE_REVISION:
                raise ValueError("resource profile revision limit reached")
            updated = ResourceProfileSelection(profile, current.revision + 1)
            self._write_budget(conn, profile)
            conn.execute(
                f"UPDATE {_SELECTION} SET profile = ?, revision = ? WHERE singleton = 1",
                (profile.value, updated.revision),
            )
            self._audit.append_with_connection(
                conn,
                event_type="desktop.resource_profile_changed",
                entity_type="resource_pool",
                entity_id=DESKTOP_RESOURCE_OWNER,
                payload={
                    "profile": profile.value,
                    "previous_profile": current.profile.value,
                    "revision": updated.revision,
                },
            )
            return updated

    def denial_reason(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        owner_id: str,
        snapshot: ResourceSnapshot,
        active_count: int,
    ) -> str | None:
        if (scope, owner_id) != (DESKTOP_RESOURCE_SCOPE, DESKTOP_RESOURCE_OWNER):
            return None
        if type(active_count) is not int or active_count < 0:
            return "invalid_profile_policy"
        current = self._read_with_connection(conn)
        decision = self._policy.evaluate(
            profile=current.profile,
            snapshot=snapshot,
            requested_workload=WorkloadClass.GENERAL,
        )
        return None if decision.allowed else decision.reason

    def _write_budget(self, conn: sqlite3.Connection, profile: ResourceProfileName) -> None:
        ResourceManager.set_budget_with_connection(
            conn,
            budget_for_profile(
                profile=profile,
                scope=DESKTOP_RESOURCE_SCOPE,
                owner_id=DESKTOP_RESOURCE_OWNER,
                policy=self._policy,
            ),
        )

    @staticmethod
    def _read_with_connection(conn: sqlite3.Connection) -> ResourceProfileSelection:
        _validate_schema(conn)
        rows = conn.execute(
            f"SELECT singleton, profile, revision FROM {_SELECTION} LIMIT 2"
        ).fetchall()
        if len(rows) != 1 or type(rows[0]["singleton"]) is not int or rows[0]["singleton"] != 1:
            raise RuntimeError("resource profile selection is corrupt")
        row = rows[0]
        if type(row["profile"]) is not str:
            raise RuntimeError("resource profile selection is corrupt")
        try:
            profile = ResourceProfileName(row["profile"])
        except ValueError as error:
            raise RuntimeError("resource profile selection is corrupt") from error
        revision = row["revision"]
        if type(revision) is not int or not 1 <= revision <= MAX_PROFILE_REVISION:
            raise RuntimeError("resource profile selection is corrupt")
        return ResourceProfileSelection(profile, revision)


def _require_profile(profile: ResourceProfileName) -> None:
    if type(profile) is not ResourceProfileName:
        raise ValueError("resource profile must be an exact ResourceProfileName")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _validate_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, _MARKER) or not _table_exists(conn, _SELECTION):
        raise RuntimeError("resource profile schema is incomplete")
    marker_shape = {
        row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({_MARKER})")
    }
    selection_shape = {
        row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({_SELECTION})")
    }
    if marker_shape != {"version": ("INTEGER", 0, 1), "applied_at": ("TEXT", 1, 0)}:
        raise RuntimeError("resource profile migration schema is corrupt")
    if selection_shape != {
        "singleton": ("INTEGER", 0, 1),
        "profile": ("TEXT", 1, 0),
        "revision": ("INTEGER", 1, 0),
    }:
        raise RuntimeError("resource profile selection schema is corrupt")
    versions = [
        row["version"]
        for row in conn.execute(f"SELECT version FROM {_MARKER} ORDER BY version LIMIT 3")
    ]
    if any(type(version) is not int for version in versions) or versions != [1]:
        raise RuntimeError("resource profile migration history is unsupported")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (_SELECTION,)
    ).fetchone()["sql"]
    if "".join(sql.lower().split()) != "".join(_SELECTION_SQL.lower().split()):
        raise RuntimeError("resource profile selection constraints are corrupt")
