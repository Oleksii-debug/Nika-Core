from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.activation_authority import ActivationAuthorityPort, ActivationSubject
from nika_core.data.sqlite import SQLiteStore
from nika_core.plugins.sdk import PluginManifest
from nika_core.workspaces.catalog import WorkspaceCatalog, WorkspaceManifest

WORKSPACE_ACTIVATION_SCHEMA_VERSION = 1

PluginManifestProvider = Callable[[], Mapping[str, PluginManifest]]


@dataclass(frozen=True, slots=True)
class StoredWorkspaceActivation:
    workspace: WorkspaceManifest
    generation: int
    plugins: tuple[PluginManifest, ...]
    effective_permission_ids: tuple[str, ...]
    status: str
    created_at: str
    activated_at: str | None


class WorkspaceActivationRepository:
    """Durable canonical M9 activation metadata; adapter code is never loaded on restart."""

    def __init__(
        self,
        store: SQLiteStore,
        catalog: WorkspaceCatalog,
        plugin_manifests: PluginManifestProvider,
        *,
        activation_authority: ActivationAuthorityPort | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._plugin_manifests = plugin_manifests
        self._activation_authority = activation_authority
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workspace_activation_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM workspace_activation_schema_migrations"
            ).fetchone()
            current = _exact_nonnegative_int(
                row["version"] or 0,
                field="workspace activation schema version",
            )
            if current > WORKSPACE_ACTIVATION_SCHEMA_VERSION:
                raise RuntimeError(
                    "workspace activation schema is newer than this Nika version"
                )
            if current < 1:
                conn.execute(
                    "CREATE TABLE workspace_activation_versions ("
                    "workspace_id TEXT NOT NULL, generation INTEGER NOT NULL "
                    "CHECK(generation > 0), "
                    "manifest_version TEXT NOT NULL, manifest_json TEXT NOT NULL, "
                    "plugins_json TEXT NOT NULL, effective_permissions_json TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN ('candidate','active','retired')), "
                    "created_at TEXT NOT NULL, activated_at TEXT, "
                    "PRIMARY KEY(workspace_id, generation))"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX workspace_activation_one_active "
                    "ON workspace_activation_versions(workspace_id) WHERE status = 'active'"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX workspace_activation_manifest_version "
                    "ON workspace_activation_versions(workspace_id, manifest_version)"
                )
                conn.execute(
                    "INSERT INTO workspace_activation_schema_migrations(version, applied_at) "
                    "VALUES (1, ?)",
                    (datetime.now(UTC).isoformat(),),
                )

    def next_generation(self, workspace_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(generation) AS generation FROM workspace_activation_versions "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return _exact_nonnegative_int(
            row["generation"] or 0,
            field="workspace activation generation",
        ) + 1

    def save_candidate(self, workspace: WorkspaceManifest) -> StoredWorkspaceActivation:
        available = dict(self._plugin_manifests())
        self._catalog.validate(workspace, available)
        selected_plugins = tuple(
            available[item.plugin_id] for item in workspace.required_plugins
        )
        manifest_json = _canonical_json(workspace.model_dump(mode="json"))
        plugins_json = _canonical_json(
            [item.model_dump(mode="json") for item in selected_plugins]
        )
        permissions = self._catalog.effective_permission_ids(workspace)
        now = datetime.now(UTC).isoformat()

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT manifest_json FROM workspace_activation_versions "
                "WHERE workspace_id = ? AND manifest_version = ?",
                (workspace.workspace_id, workspace.version),
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_json"]) != manifest_json:
                    raise ValueError(
                        "workspace manifest version is immutable and already has other content"
                    )
                raise ValueError("workspace manifest version already exists")

            row = conn.execute(
                "SELECT MAX(generation) AS generation FROM workspace_activation_versions "
                "WHERE workspace_id = ?",
                (workspace.workspace_id,),
            ).fetchone()
            generation = _exact_nonnegative_int(
                row["generation"] or 0,
                field="workspace activation generation",
            ) + 1
            conn.execute(
                "INSERT INTO workspace_activation_versions("
                "workspace_id, generation, manifest_version, manifest_json, plugins_json, "
                "effective_permissions_json, status, created_at, activated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, NULL)",
                (
                    workspace.workspace_id,
                    generation,
                    workspace.version,
                    manifest_json,
                    plugins_json,
                    _canonical_json(list(permissions)),
                    now,
                ),
            )
        stored = self.get(workspace.workspace_id, generation)
        if stored is None:
            raise RuntimeError("workspace activation candidate was not persisted")
        return stored

    def activate(
        self,
        workspace_id: str,
        generation: int,
        *,
        approval_refs: tuple[str, ...] = (),
    ) -> StoredWorkspaceActivation:
        generation = _exact_positive_int(
            generation,
            field="workspace activation generation",
        )

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT manifest_version, manifest_json, plugins_json, "
                "effective_permissions_json, status, created_at, activated_at "
                "FROM workspace_activation_versions "
                "WHERE workspace_id = ? AND generation = ?",
                (workspace_id, generation),
            ).fetchone()
            if row is None:
                raise KeyError("workspace activation candidate does not exist")
            candidate = self._decode_and_validate(workspace_id, generation, row)

            active_row = conn.execute(
                "SELECT generation FROM workspace_activation_versions "
                "WHERE workspace_id = ? AND status = 'active'",
                (workspace_id,),
            ).fetchone()
            if active_row is not None:
                active_generation = _exact_positive_int(
                    active_row["generation"],
                    field="active workspace generation",
                )
                if active_generation == generation:
                    return candidate
                if active_generation > generation:
                    raise PermissionError(
                        "stale workspace activation cannot replace a newer active generation"
                    )

            if candidate.status != "candidate":
                raise ValueError(
                    f"cannot activate workspace candidate in status {candidate.status}"
                )

            current_plugins = dict(self._plugin_manifests())
            self._catalog.validate(candidate.workspace, current_plugins)
            expected_plugins = tuple(
                current_plugins[item.plugin_id]
                for item in candidate.workspace.required_plugins
            )
            if candidate.plugins != expected_plugins:
                raise PermissionError(
                    "plugin manifest changed after workspace candidate review"
                )

            high_impact_ids = self._catalog.high_impact_ids(
                candidate.workspace,
                current_plugins,
            )
            subject = ActivationSubject.from_payload(
                kind="workspace",
                subject_id=candidate.workspace.workspace_id,
                version=f"{candidate.workspace.version}@{candidate.generation}",
                payload={
                    "workspace": candidate.workspace.model_dump(mode="json"),
                    "plugins": [
                        item.model_dump(mode="json") for item in candidate.plugins
                    ],
                },
                permission_ids=candidate.effective_permission_ids,
                high_impact_ids=high_impact_ids,
            )
            if subject.requires_authority:
                if self._activation_authority is None:
                    raise PermissionError("trusted activation authority is required")
                self._activation_authority.verify(subject, approval_refs)

            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE workspace_activation_versions SET status = 'retired' "
                "WHERE workspace_id = ? AND status = 'active'",
                (workspace_id,),
            )
            conn.execute(
                "UPDATE workspace_activation_versions "
                "SET status = 'active', activated_at = ? "
                "WHERE workspace_id = ? AND generation = ?",
                (now, workspace_id, generation),
            )

        active = self.active(workspace_id)
        if active is None or active.generation != generation:
            raise RuntimeError("workspace activation did not commit exact candidate")
        return active

    def active(self, workspace_id: str) -> StoredWorkspaceActivation | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT generation, manifest_version, manifest_json, plugins_json, "
                "effective_permissions_json, status, created_at, activated_at "
                "FROM workspace_activation_versions "
                "WHERE workspace_id = ? AND status = 'active'",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        generation = _exact_positive_int(
            row["generation"],
            field="active workspace generation",
        )
        return self._decode_and_validate(workspace_id, generation, row)

    def get(
        self,
        workspace_id: str,
        generation: int,
    ) -> StoredWorkspaceActivation | None:
        generation = _exact_positive_int(
            generation,
            field="workspace activation generation",
        )
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT manifest_version, manifest_json, plugins_json, "
                "effective_permissions_json, status, created_at, activated_at "
                "FROM workspace_activation_versions "
                "WHERE workspace_id = ? AND generation = ?",
                (workspace_id, generation),
            ).fetchone()
        if row is None:
            return None
        return self._decode_and_validate(workspace_id, generation, row)

    def _decode_and_validate(
        self,
        workspace_id: str,
        generation: int,
        row: sqlite3.Row,
    ) -> StoredWorkspaceActivation:
        stored = _decode_row(workspace_id, generation, row)
        expected_permissions = self._catalog.effective_permission_ids(stored.workspace)
        if stored.effective_permission_ids != expected_permissions:
            raise RuntimeError(
                "stored workspace effective permissions differ from manifest selection"
            )
        required_plugin_ids = tuple(
            requirement.plugin_id for requirement in stored.workspace.required_plugins
        )
        persisted_plugin_ids = tuple(plugin.plugin_id for plugin in stored.plugins)
        if persisted_plugin_ids != required_plugin_ids:
            raise RuntimeError(
                "stored workspace plugin review set differs from manifest requirements"
            )
        return stored


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_row(
    workspace_id: str,
    generation: int,
    row: sqlite3.Row,
) -> StoredWorkspaceActivation:
    manifest_json = row["manifest_json"]
    plugins_json = row["plugins_json"]
    permissions_json = row["effective_permissions_json"]
    workspace = WorkspaceManifest.model_validate(json.loads(str(manifest_json)))
    if workspace.workspace_id != workspace_id:
        raise RuntimeError("stored workspace identity does not match activation key")
    if workspace.version != str(row["manifest_version"]):
        raise RuntimeError("stored workspace manifest version does not match row identity")

    raw_plugins = json.loads(str(plugins_json))
    if not isinstance(raw_plugins, list):
        raise RuntimeError("stored workspace plugin review metadata is invalid")
    plugins = tuple(PluginManifest.model_validate(item) for item in raw_plugins)

    raw_permissions = json.loads(str(permissions_json))
    if not isinstance(raw_permissions, list) or any(
        not isinstance(item, str) or not item for item in raw_permissions
    ):
        raise RuntimeError("stored workspace effective permissions are invalid")
    permissions = tuple(raw_permissions)
    expected_permissions = tuple(sorted(set(permissions)))
    if permissions != expected_permissions:
        raise RuntimeError("stored workspace effective permissions are not canonical")

    return StoredWorkspaceActivation(
        workspace=workspace,
        generation=generation,
        plugins=plugins,
        effective_permission_ids=permissions,
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        activated_at=(
            str(row["activated_at"])
            if row["activated_at"] is not None
            else None
        ),
    )


def _exact_nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{field} must be an exact non-negative integer")
    return value


def _exact_positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive exact integer")
    return value
