from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import build_release_manifest, write_release_manifest
from nika_core.packaging.update import (
    ReleaseIdentity,
    UpdateLifecycleError,
    UpdatePhase,
    UpdateRequest,
    WindowsUpdateLifecycle,
)

_OLD_SHA = "1" * 40
_TARGET_SHA = "2" * 40


class _ProcessLoss(BaseException):
    pass


class _Health:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        assert install_dir.is_dir()
        assert database_path.is_file()
        self.calls.append(expected.source_sha)
        if expected.source_sha == _TARGET_SHA:
            raise RuntimeError("injected candidate health failure")


class _NoopMigrator:
    def migrate(self, database_path: Path) -> None:
        assert database_path.is_file()


class _CrashAtRollbackPackage(WindowsUpdateLifecycle):
    def _after_phase(self, phase: UpdatePhase) -> None:
        if phase == UpdatePhase.ROLLBACK_PACKAGE:
            raise _ProcessLoss()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _bundle(
    root: Path,
    *,
    version: str,
    source_sha: str,
    marker: str,
) -> Path:
    root.mkdir(parents=True)
    (root / "NikaCore.exe").write_bytes(marker.encode("utf-8"))
    manifest = build_release_manifest(
        root,
        product="NikaCore",
        version=version,
        source_sha=source_sha,
    )
    write_release_manifest(root, manifest)
    return root


def _archive(bundle: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return target


def test_restart_rejects_same_identity_substituted_rollback_bundle(tmp_path: Path) -> None:
    install = _bundle(
        tmp_path / "installed",
        version="0.0.1",
        source_sha=_OLD_SHA,
        marker="trusted-old",
    )
    substitute = _bundle(
        tmp_path / "same-identity-substitute",
        version="0.0.1",
        source_sha=_OLD_SHA,
        marker="substituted-old",
    )
    candidate = _bundle(
        tmp_path / "candidate",
        version="0.0.2",
        source_sha=_TARGET_SHA,
        marker="candidate",
    )
    artifact = _archive(candidate, tmp_path / "candidate.zip")
    reference = "./dist/NikaCore-0.0.2-windows-x64.zip"
    evidence = tmp_path / "candidate-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "commit_sha": _TARGET_SHA,
                "distributable_zip_path": reference,
                "distributable_zip_sha256": _sha256(artifact),
                "distributable_zip_size": artifact.stat().st_size,
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "nika.db"
    SQLiteStore(database).initialize()
    request = UpdateRequest(
        artifact_path=artifact,
        evidence_path=evidence,
        artifact_reference=reference,
        install_dir=install,
        database_path=database,
        expected_product="NikaCore",
        expected_version="0.0.2",
        expected_source_sha=_TARGET_SHA,
    )
    state = tmp_path / "state"
    health = _Health()

    with pytest.raises(_ProcessLoss):
        _CrashAtRollbackPackage(
            state_dir=state,
            health=health,
            migrator=_NoopMigrator(),
        ).run(request)

    rollback_dirs = tuple(tmp_path.glob(".installed.nika-update-*.rollback"))
    assert len(rollback_dirs) == 1
    rollback = rollback_dirs[0]
    shutil.rmtree(rollback)
    shutil.copytree(substitute, rollback)

    with pytest.raises(UpdateLifecycleError):
        WindowsUpdateLifecycle(
            state_dir=state,
            health=health,
            migrator=_NoopMigrator(),
        ).run(request)

    assert health.calls == [_TARGET_SHA]
