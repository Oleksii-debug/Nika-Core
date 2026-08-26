from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.recovery import (
    ReleaseDatabaseRecovery,
    ReleaseDatabaseRecoveryError,
)
from nika_core.reliability.backup import (
    RestorePlanStaleError,
    SQLiteRecoveryManager,
)

SOURCE_SHA = "1" * 40
CURRENT_SHA = "2" * 40
OTHER_SHA = "3" * 40


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _recovery(path: Path) -> ReleaseDatabaseRecovery:
    return ReleaseDatabaseRecovery(SQLiteRecoveryManager(_initialize(path)))


def _insert_task(path: Path, *, task_id: str, value: str) -> None:
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as connection:
        now = "2026-08-26T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, workspace_id, agent_id, state,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "eng08-a6",
                "qa",
                "READY",
                payload,
                now,
                now,
            ),
        )


def _set_task_value(path: Path, *, task_id: str, value: str) -> None:
    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (payload, task_id),
        )


def test_same_source_sha_different_database_bytes_cannot_reuse_confirmation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live" / "ніка з пробілом.db"
    recovery = _recovery(database)
    _insert_task(database, task_id="identity", value="snapshot-a")

    first_dir = tmp_path / "snapshots" / "first"
    first = recovery.create_snapshot(first_dir, source_release_sha=SOURCE_SHA)

    _set_task_value(database, task_id="identity", value="snapshot-b")
    second_dir = tmp_path / "snapshots" / "second"
    second = recovery.create_snapshot(second_dir, source_release_sha=SOURCE_SHA)

    _set_task_value(database, task_id="identity", value="current")
    first_plan = recovery.prepare_restore(
        first_dir,
        expected_source_release_sha=SOURCE_SHA,
        current_release_sha=CURRENT_SHA,
    )
    second_plan = recovery.prepare_restore(
        second_dir,
        expected_source_release_sha=SOURCE_SHA,
        current_release_sha=CURRENT_SHA,
    )

    assert first.source_release_sha == second.source_release_sha == SOURCE_SHA
    assert first.database_sha256 != second.database_sha256
    assert first_plan.confirmation_fingerprint != second_plan.confirmation_fingerprint

    with pytest.raises(PermissionError, match="confirmation"):
        recovery.restore(
            second_plan,
            confirmation_fingerprint=first_plan.confirmation_fingerprint,
        )


def test_release_metadata_identity_change_after_preview_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    recovery = _recovery(database)
    _insert_task(database, task_id="metadata", value="snapshot")
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SOURCE_SHA)
    _set_task_value(database, task_id="metadata", value="current")

    plan = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SOURCE_SHA,
        current_release_sha=CURRENT_SHA,
    )
    metadata_path = snapshot_dir / "release-database-snapshot.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["source_release_sha"] = OTHER_SHA
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDatabaseRecoveryError, match="source SHA"):
        recovery.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )


def test_canonical_manifest_identity_change_after_preview_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    recovery = _recovery(database)
    _insert_task(database, task_id="manifest", value="snapshot")
    snapshot_dir = tmp_path / "snapshot"
    recovery.create_snapshot(snapshot_dir, source_release_sha=SOURCE_SHA)
    _set_task_value(database, task_id="manifest", value="current")

    plan = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SOURCE_SHA,
        current_release_sha=CURRENT_SHA,
    )
    manifest_path = snapshot_dir / "database.sqlite3.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-08-26T23:59:59+00:00"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDatabaseRecoveryError, match="evidence does not match"):
        recovery.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )


def test_restore_plan_cannot_be_replayed_against_another_database_target(
    tmp_path: Path,
) -> None:
    original_database = tmp_path / "original" / "live.db"
    original = _recovery(original_database)
    _insert_task(original_database, task_id="target", value="snapshot")
    snapshot_dir = tmp_path / "snapshot"
    original.create_snapshot(snapshot_dir, source_release_sha=SOURCE_SHA)
    _set_task_value(original_database, task_id="target", value="current")

    plan = original.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=SOURCE_SHA,
        current_release_sha=CURRENT_SHA,
    )

    other_database = tmp_path / "other" / "live.db"
    other = _recovery(other_database)
    _insert_task(other_database, task_id="other", value="untouched")

    with pytest.raises(RestorePlanStaleError, match="target changed"):
        other.restore(
            plan,
            confirmation_fingerprint=plan.confirmation_fingerprint,
        )
