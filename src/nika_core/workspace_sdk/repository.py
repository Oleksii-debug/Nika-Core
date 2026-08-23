from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.workspace_sdk.contracts import (
    WorkspaceActivationProposal,
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
)


@dataclass(frozen=True, slots=True)
class StoredWorkspacePlugin:
    manifest: WorkspaceManifest
    entrypoint: WorkspaceEntrypointDescriptor
    effective_permission_ids: tuple[str, ...]
    status: str
    created_at: str
    activated_at: str | None


class WorkspacePluginRepository:
    """Durable immutable workspace manifest versions with atomic activation."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workspace_plugin_versions ("
                "workspace_id TEXT NOT NULL, version INTEGER NOT NULL, manifest_json TEXT NOT NULL, "
                "entrypoint_json TEXT NOT NULL, effective_permissions_json TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT, "
                "PRIMARY KEY(workspace_id, version))"
            )

    def next_version(self, workspace_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM workspace_plugin_versions WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return int(row["version"] or 0) + 1

    def save_candidate(self, proposal: WorkspaceActivationProposal) -> None:
        manifest = proposal.manifest
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM workspace_plugin_versions WHERE workspace_id = ?",
                (manifest.workspace_id,),
            ).fetchone()
            expected = int(row["version"] or 0) + 1
            if manifest.version != expected:
                raise ValueError(
                    f"workspace version must be the next immutable version: expected {expected}"
                )
            conn.execute(
                "INSERT INTO workspace_plugin_versions("
                "workspace_id, version, manifest_json, entrypoint_json, effective_permissions_json, "
                "status, created_at, activated_at) VALUES (?, ?, ?, ?, ?, 'candidate', ?, NULL)",
                (
                    manifest.workspace_id,
                    manifest.version,
                    manifest.model_dump_json(),
                    json.dumps(asdict(proposal.entrypoint), ensure_ascii=False, sort_keys=True),
                    json.dumps(proposal.effective_permission_ids, ensure_ascii=False),
                    now,
                ),
            )

    def activate(self, proposal: WorkspaceActivationProposal) -> None:
        manifest = proposal.manifest
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT manifest_json, entrypoint_json, effective_permissions_json, status "
                "FROM workspace_plugin_versions WHERE workspace_id = ? AND version = ?",
                (manifest.workspace_id, manifest.version),
            ).fetchone()
            if row is None:
                raise KeyError("workspace activation candidate does not exist")
            persisted = self._decode_row(row, created_at="", activated_at=None)
            if persisted.manifest != manifest or persisted.entrypoint != proposal.entrypoint:
                raise ValueError("workspace activation differs from persisted immutable candidate")
            if persisted.effective_permission_ids != proposal.effective_permission_ids:
                raise PermissionError("workspace effective permissions changed after approval")
            if row["status"] == "active":
                return
            if row["status"] != "candidate":
                raise ValueError(f"cannot activate workspace in status {row['status']}")
            conn.execute(
                "UPDATE workspace_plugin_versions SET status = 'retired' "
                "WHERE workspace_id = ? AND status = 'active'",
                (manifest.workspace_id,),
            )
            conn.execute(
                "UPDATE workspace_plugin_versions SET status = 'active', activated_at = ? "
                "WHERE workspace_id = ? AND version = ?",
                (now, manifest.workspace_id, manifest.version),
            )

    def active(self, workspace_id: str) -> StoredWorkspacePlugin | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT manifest_json, entrypoint_json, effective_permissions_json, status, "
                "created_at, activated_at FROM workspace_plugin_versions "
                "WHERE workspace_id = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def get(self, workspace_id: str, version: int) -> StoredWorkspacePlugin | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT manifest_json, entrypoint_json, effective_permissions_json, status, "
                "created_at, activated_at FROM workspace_plugin_versions "
                "WHERE workspace_id = ? AND version = ?",
                (workspace_id, version),
            ).fetchone()
        return self._decode(row) if row is not None else None

    @classmethod
    def _decode(cls, row) -> StoredWorkspacePlugin:
        return cls._decode_row(
            row,
            created_at=str(row["created_at"]),
            activated_at=str(row["activated_at"]) if row["activated_at"] is not None else None,
        )

    @staticmethod
    def _decode_row(row, *, created_at: str, activated_at: str | None) -> StoredWorkspacePlugin:
        descriptor = WorkspaceEntrypointDescriptor(**json.loads(row["entrypoint_json"]))
        permissions = tuple(str(item) for item in json.loads(row["effective_permissions_json"]))
        return StoredWorkspacePlugin(
            manifest=WorkspaceManifest.model_validate_json(row["manifest_json"]),
            entrypoint=descriptor,
            effective_permission_ids=permissions,
            status=str(row["status"]),
            created_at=created_at,
            activated_at=activated_at,
        )
