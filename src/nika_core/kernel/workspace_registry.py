from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

_SQLITE_MAX_INT64 = (1 << 63) - 1


def _text_value(value: object, field_name: str, *, allow_blank: bool) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    if not allow_blank and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _stored_text(value: object, field_name: str, *, allow_blank: bool) -> str:
    try:
        return _text_value(value, field_name, allow_blank=allow_blank)
    except ValueError as exc:
        raise ValueError(f"invalid persisted workspace {field_name}") from exc


def _reject_corrupt_identity_alias(conn: object, workspace_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM workspaces WHERE typeof(workspace_id) != 'text' "
        "AND CAST(workspace_id AS TEXT) = ? LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if row is not None:
        raise ValueError("invalid persisted workspace workspace_id")


@dataclass(frozen=True, slots=True)
class WorkspaceDefinition:
    workspace_id: str
    name: str
    version: int
    description: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        _text_value(self.workspace_id, "workspace_id", allow_blank=False)
        _text_value(self.name, "name", allow_blank=False)
        if type(self.version) is not int or not 1 <= self.version <= _SQLITE_MAX_INT64:
            raise ValueError("version must be a positive SQLite-sized integer")
        _text_value(self.description, "description", allow_blank=True)
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")


def _snapshot_definition(value: object) -> WorkspaceDefinition:
    if type(value) is not WorkspaceDefinition:
        raise TypeError("definition must be an exact WorkspaceDefinition")
    return WorkspaceDefinition(
        value.workspace_id,
        value.name,
        value.version,
        description=value.description,
        enabled=value.enabled,
    )


class WorkspaceRegistry:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    @property
    def count(self) -> int:
        with self._store.connection() as conn:
            corrupt = conn.execute(
                "SELECT 1 FROM workspaces WHERE typeof(workspace_id) != 'text' LIMIT 1"
            ).fetchone()
            if corrupt is not None:
                raise ValueError("invalid persisted workspace workspace_id")
            row = conn.execute(
                "SELECT COUNT(DISTINCT workspace_id) AS count FROM workspaces"
            ).fetchone()
        return int(row["count"])

    def register(self, definition: WorkspaceDefinition) -> None:
        canonical = _snapshot_definition(definition)
        with self._store.connection() as conn:
            # Serialize the version check and insert across independent registry instances.
            # A deferred transaction would still allow multiple writers to observe the same
            # previous version before one of them commits.
            conn.execute("BEGIN IMMEDIATE")
            _reject_corrupt_identity_alias(conn, canonical.workspace_id)
            row = conn.execute(
                "SELECT workspace_id, name, version, description, enabled "
                "FROM workspaces WHERE workspace_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (canonical.workspace_id,),
            ).fetchone()
            if row is not None:
                _stored_text(row["workspace_id"], "workspace_id", allow_blank=False)
                _stored_text(row["name"], "name", allow_blank=False)
                current_version = _stored_version(row["version"])
                _stored_text(row["description"], "description", allow_blank=True)
                _stored_enabled(row["enabled"])
                if canonical.version <= current_version:
                    raise ValueError("workspace version must increase")
            conn.execute(
                "INSERT INTO workspaces(workspace_id, version, name, description, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    canonical.workspace_id,
                    canonical.version,
                    canonical.name,
                    canonical.description,
                    int(canonical.enabled),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, workspace_id: str) -> WorkspaceDefinition:
        normalized_id = _text_value(workspace_id, "workspace_id", allow_blank=False)
        current = self._latest(normalized_id)
        if current is None:
            raise KeyError(f"Unknown workspace: {normalized_id}")
        return current

    def list_latest(self) -> tuple[WorkspaceDefinition, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT w.workspace_id, w.name, w.version, w.description, w.enabled "
                "FROM workspaces AS w "
                "JOIN (SELECT workspace_id, MAX(version) AS version FROM workspaces GROUP BY workspace_id) AS latest "
                "ON latest.workspace_id = w.workspace_id AND latest.version = w.version "
                "ORDER BY w.workspace_id"
            ).fetchall()
        return tuple(
            WorkspaceDefinition(
                workspace_id=_stored_text(
                    row["workspace_id"], "workspace_id", allow_blank=False
                ),
                name=_stored_text(row["name"], "name", allow_blank=False),
                version=_stored_version(row["version"]),
                description=_stored_text(
                    row["description"], "description", allow_blank=True
                ),
                enabled=_stored_enabled(row["enabled"]),
            )
            for row in rows
        )

    def _latest(self, workspace_id: str) -> WorkspaceDefinition | None:
        with self._store.connection() as conn:
            _reject_corrupt_identity_alias(conn, workspace_id)
            row = conn.execute(
                "SELECT workspace_id, name, version, description, enabled FROM workspaces "
                "WHERE workspace_id = ? ORDER BY version DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceDefinition(
            workspace_id=_stored_text(row["workspace_id"], "workspace_id", allow_blank=False),
            name=_stored_text(row["name"], "name", allow_blank=False),
            version=_stored_version(row["version"]),
            description=_stored_text(row["description"], "description", allow_blank=True),
            enabled=_stored_enabled(row["enabled"]),
        )


def _stored_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _SQLITE_MAX_INT64:
        raise ValueError("invalid persisted workspace version")
    return value


def _stored_enabled(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("invalid persisted workspace enabled flag")
    return bool(value)
