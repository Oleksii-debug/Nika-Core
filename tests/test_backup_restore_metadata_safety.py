"""Fail-closed recovery metadata tests against real SQLite backup/restore paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.backup import (
    BackupVerificationError,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)


def _manager(path: Path) -> SQLiteRecoveryManager:
    store = SQLiteStore(path)
    store.initialize()
    return SQLiteRecoveryManager(store)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("format_version", True),
        ("format_version", 1.0),
        ("format_version", "1"),
        ("size_bytes", lambda value: str(value)),
        ("size_bytes", lambda value: float(value)),
        ("size_bytes", True),
        ("schema_version", lambda value: str(value)),
        ("schema_version", lambda value: float(value)),
        ("schema_version", True),
    ],
)
def test_backup_rejects_noncanonical_manifest_types(
    tmp_path: Path, field: str, replacement: object
) -> None:
    manager = _manager(tmp_path / "live.db")
    artifact = manager.create_backup(tmp_path / "backup.sqlite3", record_audit=False)
    assert manager.verify_backup(artifact.database_path) == artifact
    backup_sha = _digest(artifact.database_path)
    live_sha = _digest(tmp_path / "live.db")
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    original = manifest[field]
    manifest[field] = replacement(original) if callable(replacement) else replacement
    artifact.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupVerificationError):
        manager.verify_backup(artifact.database_path)
    with pytest.raises(BackupVerificationError):
        manager.prepare_restore(artifact.database_path)
    assert _digest(artifact.database_path) == backup_sha
    assert _digest(tmp_path / "live.db") == live_sha


def test_backup_rejects_duplicate_manifest_key_even_if_values_agree(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path / "live.db")
    artifact = manager.create_backup(tmp_path / "backup.sqlite3", record_audit=False)
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    original_sha = _digest(artifact.database_path)
    repeated = json.dumps(manifest)[:-1] + (
        ', "size_bytes": ' + json.dumps(manifest["size_bytes"]) + "}"
    )
    artifact.manifest_path.write_text(repeated, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager.verify_backup(artifact.database_path)
    assert _digest(artifact.database_path) == original_sha


def _marker(manager: SQLiteRecoveryManager, target: Path) -> dict[str, object]:
    quarantine = manager._quarantine_name(target)
    return {
        "format_version": 1,
        "target_file": target.name,
        "stage_file": manager._temporary_path(target, "restore-stage").name,
        "stage_sha256": _digest(target),
        "quarantine_file": quarantine,
        "quarantine_wal_file": quarantine + "-wal",
        "quarantine_shm_file": quarantine + "-shm",
        "current_sha256": "0" * 64,
        "backup_sha256": "1" * 64,
        "created_at": "2026-10-04T12:00:00+00:00",
    }


def test_canonical_restore_marker_names_remain_accepted(tmp_path: Path) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    marker = _marker(manager, target)
    marker_path = manager._restore_marker_path(target)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert manager._read_restore_marker(marker_path, target) == marker


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("format_version", True),
        ("format_version", 1.0),
        ("format_version", "1"),
        ("stage_file", "user-notes.txt"),
        ("stage_file", ".."),
        ("quarantine_file", ".."),
        ("quarantine_file", "user-notes.txt"),
        ("quarantine_wal_file", "user-notes.txt"),
        ("quarantine_shm_file", "user-notes.txt"),
    ],
)
def test_forged_restore_marker_cannot_delete_or_move_sibling_files(
    tmp_path: Path, field: str, replacement: object
) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    live_sha = _digest(target)
    sentinel = tmp_path / "user-notes.txt"
    sentinel.write_text("Do not delete me", encoding="utf-8")
    marker = _marker(manager, target)
    marker[field] = replacement
    marker_path = manager._restore_marker_path(target)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(RestoreSafetyError):
        manager.recover_interrupted_restore()
    assert _digest(target) == live_sha
    assert sentinel.read_text(encoding="utf-8") == "Do not delete me"
    assert marker_path.exists()


def test_duplicate_restore_marker_field_fails_before_effects(
    tmp_path: Path,
) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    before_sha = _digest(target)
    marker = _marker(manager, target)
    marker_path = manager._restore_marker_path(target)
    repeated = json.dumps(marker)[:-1] + ', "target_file": "live.db"}'
    marker_path.write_text(repeated, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager.recover_interrupted_restore()
    assert _digest(target) == before_sha
    assert marker_path.exists()


@pytest.mark.parametrize("kind", ["symlink", "broken_symlink", "directory"])
def test_restore_marker_path_must_be_direct_regular_file(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    before_sha = _digest(target)
    marker_path = manager._restore_marker_path(target)

    if kind == "directory":
        marker_path.mkdir()
    else:
        source = tmp_path / ("marker-source.json" if kind == "symlink" else "missing.json")
        if kind == "symlink":
            source.write_text(json.dumps(_marker(manager, target)), encoding="utf-8")
        try:
            marker_path.symlink_to(source)
        except (NotImplementedError, OSError):
            pytest.skip("filesystem does not allow creation of this symlink")

    with pytest.raises(RestoreSafetyError, match="direct regular file"):
        manager.recover_interrupted_restore()
    assert _digest(target) == before_sha
    if kind == "broken_symlink":
        assert marker_path.is_symlink()
        assert not marker_path.exists()
    else:
        assert marker_path.exists()


def test_broken_restore_marker_blocks_new_restore_preview(tmp_path: Path) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    before_sha = _digest(target)
    artifact = manager.create_backup(tmp_path / "backup.sqlite3", record_audit=False)
    marker_path = manager._restore_marker_path(target)
    try:
        marker_path.symlink_to(tmp_path / "missing-marker.json")
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not allow creation of this symlink")

    with pytest.raises(RestoreSafetyError, match="direct regular file"):
        manager.prepare_restore(artifact.database_path)
    assert marker_path.is_symlink()
    assert _digest(target) == before_sha


def test_json_metadata_reader_rejects_indirect_file_even_without_caller_precheck(
    tmp_path: Path,
) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    source = tmp_path / "metadata-source.json"
    source.write_text('{"format_version": 1}', encoding="utf-8")
    link = tmp_path / "metadata-link.json"
    try:
        link.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not allow creation of this symlink")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager._read_json(link)


@pytest.mark.parametrize("kind", ["backup", "marker"])
def test_recovery_metadata_size_is_bounded_even_with_json_whitespace(
    tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    if kind == "backup":
        artifact = manager.create_backup(tmp_path / "backup.sqlite3", record_audit=False)
        metadata = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
        path = artifact.manifest_path
    else:
        metadata = _marker(manager, target)
        path = manager._restore_marker_path(target)
    original_sha = _digest(target)
    path.write_text(json.dumps(metadata) + " " * 65_537, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        if kind == "backup":
            manager.verify_backup(artifact.database_path)
        else:
            manager._read_restore_marker(path, target)
    assert _digest(target) == original_sha


def test_deeply_nested_restore_metadata_is_a_typed_failure(tmp_path: Path) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    original_sha = _digest(target)
    marker_path = manager._restore_marker_path(target)
    marker_path.write_text("[" * 1_500 + "0" + "]" * 1_500, encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="JSON recovery metadata"):
        manager.recover_interrupted_restore()
    assert _digest(target) == original_sha
    assert marker_path.exists()


@pytest.mark.parametrize(
    "artifact_key",
    [
        "stage_file",
        "quarantine_file",
        "quarantine_wal_file",
        "quarantine_shm_file",
        "quarantine_manifest",
    ],
)
def test_marker_rejects_indirect_recovery_artifacts_before_effects(
    tmp_path: Path, artifact_key: str
) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    before_sha = _digest(target)
    sentinel = tmp_path / "user-notes.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    marker = _marker(manager, target)
    name = (
        manager._manifest_path(tmp_path / str(marker["quarantine_file"])).name
        if artifact_key == "quarantine_manifest"
        else str(marker[artifact_key])
    )
    try:
        (tmp_path / name).symlink_to(sentinel)
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not allow creation of this symlink")
    marker_path = manager._restore_marker_path(target)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(RestoreSafetyError, match="indirect or not a regular file"):
        manager.recover_interrupted_restore()
    assert _digest(target) == before_sha
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert marker_path.exists()


def test_marker_rejects_nonregular_stage_before_effects(tmp_path: Path) -> None:
    target = tmp_path / "live.db"
    manager = _manager(target)
    before_sha = _digest(target)
    marker = _marker(manager, target)
    (tmp_path / str(marker["stage_file"])).mkdir()
    marker_path = manager._restore_marker_path(target)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(RestoreSafetyError, match="indirect or not a regular file"):
        manager.recover_interrupted_restore()
    assert _digest(target) == before_sha
    assert marker_path.exists()
