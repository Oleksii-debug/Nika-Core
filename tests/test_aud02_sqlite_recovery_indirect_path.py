from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.reliability.backup as backup_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.reliability.backup import (
    BackupVerificationError,
    RestoreSafetyError,
    SQLiteRecoveryManager,
)


def _initialize(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.name)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"filesystem cannot create test symlink: {exc}")
    if not link.is_symlink():
        pytest.skip("filesystem did not preserve a symlink identity")


def test_verify_backup_rejects_top_level_symlink_source_identity(tmp_path: Path) -> None:
    """Resolving the caller path first must not erase an indirect backup identity."""

    live = _initialize(tmp_path / "live.db")
    manager = SQLiteRecoveryManager(live)
    backup = tmp_path / "canonical-backup.db"
    manager.create_backup(backup)

    alias = tmp_path / "candidate-backup-alias.db"
    _symlink_or_skip(alias, backup)
    assert alias.resolve() == backup.resolve()

    with pytest.raises(
        BackupVerificationError,
        match="direct files|indirect",
    ):
        manager.verify_backup(alias)


def test_prepare_restore_rejects_top_level_symlink_target_identity(tmp_path: Path) -> None:
    """The configured live target itself must cross the indirect-path guard."""

    source = _initialize(tmp_path / "source.db")
    backup = tmp_path / "known-good.db"
    SQLiteRecoveryManager(source).create_backup(backup)

    canonical_target = tmp_path / "canonical-target.db"
    _initialize(canonical_target)
    alias_target = tmp_path / "configured-target-alias.db"
    _symlink_or_skip(alias_target, canonical_target)
    assert alias_target.resolve() == canonical_target.resolve()

    manager = SQLiteRecoveryManager(SQLiteStore(alias_target))
    with pytest.raises(RestoreSafetyError, match="indirect filesystem path"):
        manager.prepare_restore(backup)


def test_create_backup_rejects_top_level_symlink_destination_before_publication(
    tmp_path: Path,
) -> None:
    live = _initialize(tmp_path / "live-create.db")
    manager = SQLiteRecoveryManager(live)
    redirected = tmp_path / "redirected-backup.db"
    alias = tmp_path / "requested-backup-alias.db"
    _symlink_or_skip(alias, redirected)
    assert alias.resolve() == redirected.resolve()

    with pytest.raises(RestoreSafetyError, match="backup destination.*indirect"):
        manager.create_backup(alias)

    assert not redirected.exists()
    assert not redirected.with_name(f"{redirected.name}.manifest.json").exists()


def test_create_backup_rejects_top_level_symlink_live_source_before_side_effects(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical-live.db"
    _initialize(canonical)
    alias = tmp_path / "configured-live-alias.db"
    _symlink_or_skip(alias, canonical)
    assert alias.resolve() == canonical.resolve()

    backup = tmp_path / "must-not-exist.db"
    manager = SQLiteRecoveryManager(SQLiteStore(alias))
    with pytest.raises(RestoreSafetyError, match="recovery target.*indirect"):
        manager.create_backup(backup)

    assert not backup.exists()
    assert not backup.with_name(f"{backup.name}.manifest.json").exists()
    assert not canonical.with_name(f".{canonical.name}.nika-recovery.lock").exists()


def test_create_backup_rejects_windows_reparse_destination_without_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _initialize(tmp_path / "live-reparse.db")
    manager = SQLiteRecoveryManager(live)
    destination = tmp_path / "reparse-destination.db"
    real_lstat = backup_module.os.lstat
    reparse_flag = 0x400

    def fake_lstat(path):
        if Path(path) == destination:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_file_attributes=reparse_flag,
            )
        return real_lstat(path)

    monkeypatch.setattr(
        backup_module.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )
    monkeypatch.setattr(backup_module.os, "lstat", fake_lstat)

    with pytest.raises(RestoreSafetyError, match="backup destination.*indirect"):
        manager.create_backup(destination)

    assert not destination.exists()
    assert not destination.with_name(f"{destination.name}.manifest.json").exists()
