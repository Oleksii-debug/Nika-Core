from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import build_release_manifest, write_release_manifest
from nika_core.packaging.update import (
    CandidateVerificationError,
    ReleaseIdentity,
    UpdatePhase,
    UpdateRequest,
    UpdateRollbackError,
    WindowsUpdateLifecycle,
)
from nika_core.reliability import backup as backup_module

_OLD_SHA = "1" * 40
_TARGET_SHA = "2" * 40
_OTHER_SHA = "3" * 40


class _SimulatedProcessLoss(BaseException):
    pass


class _Health:
    def __init__(self, *, fail_source: str | None = None) -> None:
        self.fail_source = fail_source
        self.calls: list[str] = []

    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        assert (install_dir / "release-manifest.json").is_file()
        assert database_path.is_file()
        self.calls.append(expected.source_sha)
        if expected.source_sha == self.fail_source:
            raise RuntimeError("injected startup failure")


class _Migrator:
    def __init__(self, *, fail: bool = False, corrupt: bool = False) -> None:
        self.fail = fail
        self.corrupt = corrupt
        self.calls = 0

    def migrate(self, database_path: Path) -> None:
        self.calls += 1
        if self.corrupt:
            database_path.write_bytes(b"corrupt-update-database" * 101)
            raise RuntimeError("injected corrupt migration")
        _set_probe(database_path, "migrated")
        if self.fail:
            raise RuntimeError("injected migration failure")
        SQLiteStore(database_path).initialize()


class _CrashAtPhase(WindowsUpdateLifecycle):
    def __init__(self, *, crash_phase: UpdatePhase, **kwargs) -> None:
        super().__init__(**kwargs)
        self._crash_phase = crash_phase
        self._fired = False

    def _after_phase(self, phase: UpdatePhase) -> None:
        if phase == self._crash_phase and not self._fired:
            self._fired = True
            raise _SimulatedProcessLoss()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _make_bundle(
    root: Path,
    *,
    version: str,
    source_sha: str,
    marker: str,
) -> Path:
    root.mkdir(parents=True)
    (root / "NikaCore.exe").write_bytes((marker * 19).encode("utf-8"))
    (root / "version.txt").write_text(version, encoding="utf-8")
    manifest = build_release_manifest(
        root,
        product="NikaCore",
        version=version,
        source_sha=source_sha,
    )
    write_release_manifest(root, manifest)
    return root


def _zip_bundle(bundle: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return target


def _evidence(artifact: Path, target: Path, *, source_sha: str, reference: str) -> Path:
    payload = {
        "commit_sha": source_sha,
        "distributable_zip_path": reference,
        "distributable_zip_sha256": _sha256(artifact),
        "distributable_zip_size": artifact.stat().st_size,
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _database(path: Path) -> Path:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute("CREATE TABLE update_probe(value TEXT NOT NULL)")
        conn.execute("INSERT INTO update_probe(value) VALUES ('old')")
    return path


def _set_probe(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE update_probe SET value = ?", (value,))
        conn.commit()


def _probe(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value FROM update_probe").fetchone()
    assert row is not None
    return str(row[0])


def _fixture(tmp_path: Path) -> tuple[UpdateRequest, Path]:
    install = _make_bundle(
        tmp_path / "installed old Nika",
        version="0.0.1",
        source_sha=_OLD_SHA,
        marker="old",
    )
    candidate_bundle = _make_bundle(
        tmp_path / "candidate",
        version="0.0.2",
        source_sha=_TARGET_SHA,
        marker="new",
    )
    artifact = _zip_bundle(candidate_bundle, tmp_path / "NikaCore-0.0.2.zip")
    reference = "./dist/NikaCore-0.0.2-windows-x64.zip"
    evidence = _evidence(
        artifact,
        tmp_path / "candidate-evidence.json",
        source_sha=_TARGET_SHA,
        reference=reference,
    )
    database = _database(tmp_path / "durable data" / "nika.db")
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
    return request, tmp_path / "updater state"


def _installed_source(install: Path) -> str:
    payload = json.loads((install / "release-manifest.json").read_text(encoding="utf-8"))
    return str(payload["source_sha"])


def test_successful_update_binds_verified_candidate_backup_migration_and_health(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    health = _Health()
    migrator = _Migrator()

    result = WindowsUpdateLifecycle(
        state_dir=state,
        health=health,
        migrator=migrator,
    ).run(request)

    assert result.phase == UpdatePhase.COMPLETED
    assert result.installed.source_sha == _TARGET_SHA
    assert _installed_source(request.install_dir) == _TARGET_SHA
    assert _probe(request.database_path) == "migrated"
    assert result.pre_update_backup.is_file()
    assert result.pre_update_backup.with_name(
        f"{result.pre_update_backup.name}.manifest.json"
    ).is_file()
    assert result.rollback_package.is_dir()
    assert _installed_source(result.rollback_package) == _OLD_SHA
    assert migrator.calls == 1
    assert health.calls == [_TARGET_SHA]


def test_crash_after_backup_resumes_without_duplicate_backup_or_migration(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    health = _Health()
    migrator = _Migrator()
    crashing = _CrashAtPhase(
        state_dir=state,
        health=health,
        migrator=migrator,
        crash_phase=UpdatePhase.BACKUP_CREATED,
    )

    with pytest.raises(_SimulatedProcessLoss):
        crashing.run(request)

    backups = tuple((state / "operations").rglob("pre-update-*.sqlite3"))
    assert len(backups) == 1
    result = WindowsUpdateLifecycle(
        state_dir=state,
        health=health,
        migrator=migrator,
    ).run(request)

    assert result.phase == UpdatePhase.COMPLETED
    assert tuple((state / "operations").rglob("pre-update-*.sqlite3")) == backups
    assert migrator.calls == 1


def test_corrupt_candidate_is_rejected_before_backup(tmp_path: Path) -> None:
    request, state = _fixture(tmp_path)
    request.artifact_path.write_bytes(request.artifact_path.read_bytes() + b"tamper")

    with pytest.raises(CandidateVerificationError, match="outer evidence"):
        WindowsUpdateLifecycle(state_dir=state, health=_Health()).run(request)

    assert not tuple((state / "operations").rglob("pre-update-*.sqlite3"))
    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _probe(request.database_path) == "old"


def test_wrong_source_sha_is_rejected_before_backup(tmp_path: Path) -> None:
    request, state = _fixture(tmp_path)
    request = UpdateRequest(
        **{
            **request.__dict__,
            "expected_source_sha": _OTHER_SHA,
        }
    )

    with pytest.raises(CandidateVerificationError, match="outer evidence"):
        WindowsUpdateLifecycle(state_dir=state, health=_Health()).run(request)

    assert not tuple((state / "operations").rglob("pre-update-*.sqlite3"))


def test_old_stale_zip_cannot_substitute_for_authorized_target(tmp_path: Path) -> None:
    request, state = _fixture(tmp_path)
    stale_bundle = _make_bundle(
        tmp_path / "stale",
        version="0.0.1",
        source_sha=_OTHER_SHA,
        marker="stale",
    )
    stale_zip = _zip_bundle(stale_bundle, tmp_path / "stale.zip")
    stale_evidence = _evidence(
        stale_zip,
        tmp_path / "stale-evidence.json",
        source_sha=_OTHER_SHA,
        reference=request.artifact_reference,
    )
    request = UpdateRequest(
        artifact_path=stale_zip,
        evidence_path=stale_evidence,
        artifact_reference=request.artifact_reference,
        install_dir=request.install_dir,
        database_path=request.database_path,
        expected_product=request.expected_product,
        expected_version=request.expected_version,
        expected_source_sha=request.expected_source_sha,
    )

    with pytest.raises(CandidateVerificationError, match="outer evidence"):
        WindowsUpdateLifecycle(state_dir=state, health=_Health()).run(request)

    assert _installed_source(request.install_dir) == _OLD_SHA


def test_provenance_reference_mismatch_is_rejected(tmp_path: Path) -> None:
    request, state = _fixture(tmp_path)
    request = UpdateRequest(
        artifact_path=request.artifact_path,
        evidence_path=request.evidence_path,
        artifact_reference="./dist/another.zip",
        install_dir=request.install_dir,
        database_path=request.database_path,
        expected_product=request.expected_product,
        expected_version=request.expected_version,
        expected_source_sha=request.expected_source_sha,
    )

    with pytest.raises(CandidateVerificationError, match="outer evidence"):
        WindowsUpdateLifecycle(state_dir=state, health=_Health()).run(request)


def test_migration_failure_restores_database_and_restarts_old_package(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    health = _Health()
    result = WindowsUpdateLifecycle(
        state_dir=state,
        health=health,
        migrator=_Migrator(fail=True),
    ).run(request)

    assert result.phase == UpdatePhase.ROLLED_BACK
    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _probe(request.database_path) == "old"
    assert health.calls == [_OLD_SHA]
    assert "database migration failed" in (result.failure_reason or "")


def test_startup_failure_restores_package_and_data_then_restarts_old(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    health = _Health(fail_source=_TARGET_SHA)
    result = WindowsUpdateLifecycle(
        state_dir=state,
        health=health,
        migrator=_Migrator(),
    ).run(request)

    assert result.phase == UpdatePhase.ROLLED_BACK
    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _probe(request.database_path) == "old"
    assert health.calls == [_TARGET_SHA, _OLD_SHA]
    assert "startup/health failed" in (result.failure_reason or "")


def test_stale_rollback_confirmation_fails_closed_without_reauthorization(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    crashing = _CrashAtPhase(
        state_dir=state,
        health=_Health(),
        migrator=_Migrator(fail=True),
        crash_phase=UpdatePhase.ROLLBACK_DATA_PREPARED,
    )
    with pytest.raises(_SimulatedProcessLoss):
        crashing.run(request)

    _set_probe(request.database_path, "external-writer-after-preview")
    with pytest.raises(UpdateRollbackError, match="stale"):
        WindowsUpdateLifecycle(state_dir=state, health=_Health()).run(request)

    assert _probe(request.database_path) == "external-writer-after-preview"
    assert _installed_source(request.install_dir) == _OLD_SHA


def test_interrupted_canonical_restore_is_recovered_on_updater_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, state = _fixture(tmp_path)
    health = _Health()
    real_replace = os.replace

    def process_loss_replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            ".restore-stage." in source_path.name
            and destination_path == request.database_path
        ):
            raise _SimulatedProcessLoss()
        real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", process_loss_replace)
    with pytest.raises(_SimulatedProcessLoss):
        WindowsUpdateLifecycle(
            state_dir=state,
            health=health,
            migrator=_Migrator(corrupt=True),
        ).run(request)

    marker = request.database_path.with_name(
        f".{request.database_path.name}.restore-in-progress.json"
    )
    assert marker.is_file()
    assert not request.database_path.exists()

    monkeypatch.setattr(backup_module.os, "replace", real_replace)
    result = WindowsUpdateLifecycle(state_dir=state, health=health).run(request)

    assert result.phase == UpdatePhase.ROLLED_BACK
    assert _probe(request.database_path) == "old"
    assert not marker.exists()
    assert health.calls == [_OLD_SHA]


def test_repeated_completed_invocation_is_idempotent(tmp_path: Path) -> None:
    request, state = _fixture(tmp_path)
    health = _Health()
    migrator = _Migrator()
    updater = WindowsUpdateLifecycle(
        state_dir=state,
        health=health,
        migrator=migrator,
    )
    first = updater.run(request)
    second = updater.run(request)

    assert first == second
    assert migrator.calls == 1
    assert health.calls == [_TARGET_SHA]


def test_unicode_and_space_paths_complete_update(tmp_path: Path) -> None:
    root = tmp_path / "каталог оновлення зі spaces"
    root.mkdir()
    request, state = _fixture(root)
    result = WindowsUpdateLifecycle(
        state_dir=state,
        health=_Health(),
        migrator=_Migrator(),
    ).run(request)

    assert result.phase == UpdatePhase.COMPLETED
    assert _installed_source(request.install_dir) == _TARGET_SHA
