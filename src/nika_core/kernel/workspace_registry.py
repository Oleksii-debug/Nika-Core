from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class WorkspaceDefinition:
    workspace_id: str
    name: str
    version: int
    description: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if self.version < 1:
            raise ValueError("version must be >= 1")


class WorkspaceRegistry:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    @property
    def count(self) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT workspace_id) AS count FROM workspaces"
            ).fetchone()
        return int(row["count"])

    def register(self, definition: WorkspaceDefinition) -> None:
        current = self._latest(definition.workspace_id)
        if current is not None and definition.version <= current.version:
            raise ValueError("workspace version must increase")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id, version, name, description, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    definition.workspace_id,
                    definition.version,
                    definition.name,
                    definition.description,
                    int(definition.enabled),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, workspace_id: str) -> WorkspaceDefinition:
        current = self._latest(workspace_id)
        if current is None:
            raise KeyError(f"Unknown workspace: {workspace_id}")
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
                workspace_id=row["workspace_id"],
                name=row["name"],
                version=int(row["version"]),
                description=row["description"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        )

    def _latest(self, workspace_id: str) -> WorkspaceDefinition | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT workspace_id, name, version, description, enabled FROM workspaces "
                "WHERE workspace_id = ? ORDER BY version DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceDefinition(
            workspace_id=row["workspace_id"],
            name=row["name"],
            version=int(row["version"]),
            description=row["description"],
            enabled=bool(row["enabled"]),
        )
