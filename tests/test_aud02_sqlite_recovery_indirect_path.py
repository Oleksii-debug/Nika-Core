from __future__ import annotations

from pathlib import Path

import pytest

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
