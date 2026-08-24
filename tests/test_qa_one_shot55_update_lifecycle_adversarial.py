from __future__ import annotations

import hashlib
import json
import shutil
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
    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        assert install_dir.is_dir()
        assert database_path.is_file()
        assert _installed_source(install_dir) == expected.source_sha


class _Migrator:
    def migrate(self, database_path: Path) -> None:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("UPDATE update_probe SET value = 'migrated'")
            connection.commit()
        SQLiteStore(database_path).initialize()


class _MutatePrivateCandidateAfterVerification(WindowsUpdateLifecycle):
    def __init__(self, *, replacement: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._replacement = replacement
        self._mutated = False

    def _verify_candidate_files(
        self,
        artifact: Path,
        evidence: Path,
        request: UpdateRequest,
    ) -> None:
        super()._verify_candidate_files(artifact, evidence, request)
        if not self._mutated:
            self._mutated = True
            shutil.copyfile(self._replacement, artifact)


class _CrashAfterCandidateVerified(WindowsUpdateLifecycle):
    def _after_phase(self, phase: UpdatePhase) -> None:
        if phase == UpdatePhase.CANDIDATE_VERIFIED:
            raise _ProcessLoss()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _make_bundle(
    root: Path,
    *,
    source_sha: str,
    marker: str,
) -> Path:
    root.mkdir(parents=True)
    (root / "NikaCore.exe").write_bytes((marker * 31).encode("utf-8"))
    (root / "payload.txt").write_text(marker, encoding="utf-8")
    manifest = build_release_manifest(
        root,
        product="NikaCore",
        version="0.0.2" if source_sha == _TARGET_SHA else "0.0.1",
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


def _database(path: Path) -> Path:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as connection:
        connection.execute("CREATE TABLE update_probe(value TEXT NOT NULL)")
        connection.execute("INSERT INTO update_probe(value) VALUES ('old')")
    return path


def _evidence(
    artifact: Path,
    path: Path,
    *,
    reference: str,
) -> Path:
    path.write_text(
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
    return path


def _fixture(tmp_path: Path) -> tuple[UpdateRequest, Path]:
    install = _make_bundle(
        tmp_path / "installed",
        source_sha=_OLD_SHA,
        marker="old-package",
    )
    candidate = _make_bundle(
        tmp_path / "candidate",
        source_sha=_TARGET_SHA,
        marker="authorized-candidate",
    )
    artifact = _zip_bundle(candidate, tmp_path / "candidate.zip")
    reference = "./dist/NikaCore-0.0.2-windows-x64.zip"
    evidence = _evidence(
        artifact,
        tmp_path / "candidate-evidence.json",
        reference=reference,
    )
    database = _database(tmp_path / "data" / "nika.db")
    return (
        UpdateRequest(
            artifact_path=artifact,
            evidence_path=evidence,
            artifact_reference=reference,
            install_dir=install,
            database_path=database,
            expected_product="NikaCore",
            expected_version="0.0.2",
            expected_source_sha=_TARGET_SHA,
        ),
        tmp_path / "state",
    )


def _installed_source(install_dir: Path) -> str:
    payload = json.loads(
        (install_dir / "release-manifest.json").read_text(encoding="utf-8")
    )
    return str(payload["source_sha"])


def _installed_payload(install_dir: Path) -> str:
    return (install_dir / "payload.txt").read_text(encoding="utf-8")


def _probe(database_path: Path) -> str:
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute("SELECT value FROM update_probe").fetchone()
    assert row is not None
    return str(row[0])


def test_verified_private_candidate_cannot_change_before_extraction(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    alternate = _make_bundle(
        tmp_path / "alternate",
        source_sha=_TARGET_SHA,
        marker="unbound-alternate",
    )
    alternate_zip = _zip_bundle(alternate, tmp_path / "alternate.zip")

    updater = _MutatePrivateCandidateAfterVerification(
        state_dir=state,
        health=_Health(),
        migrator=_Migrator(),
        replacement=alternate_zip,
    )

    with pytest.raises(CandidateVerificationError, match="candidate.*changed|evidence"):
        updater.run(request)

    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _installed_payload(request.install_dir) == "old-package"
    assert _probe(request.database_path) == "old"


def test_state_directory_inside_install_tree_is_rejected_before_effect(
    tmp_path: Path,
) -> None:
    request, _state = _fixture(tmp_path)
    state_inside_install = request.install_dir / "updater-state"

    updater = WindowsUpdateLifecycle(
        state_dir=state_inside_install,
        health=_Health(),
        migrator=_Migrator(),
    )

    with pytest.raises(UpdateLifecycleError, match="state.*install|install.*state"):
        updater.run(request)

    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _installed_payload(request.install_dir) == "old-package"
    assert _probe(request.database_path) == "old"


def test_journal_operation_identity_mismatch_fails_before_namespace_use(
    tmp_path: Path,
) -> None:
    request, state = _fixture(tmp_path)
    updater = _CrashAfterCandidateVerified(
        state_dir=state,
        health=_Health(),
        migrator=_Migrator(),
    )

    with pytest.raises(_ProcessLoss):
        updater.run(request)

    journals = tuple(state.glob("update-*.json"))
    assert len(journals) == 1
    journal_path = journals[0]
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["operation_id"] = "f" * 32
    journal_path.write_text(json.dumps(payload), encoding="utf-8")

    restarted = WindowsUpdateLifecycle(
        state_dir=state,
        health=_Health(),
        migrator=_Migrator(),
    )
    with pytest.raises(UpdateLifecycleError, match="operation.*identity|journal.*operation") as error:
        restarted.run(request)

    assert not isinstance(error.value, CandidateVerificationError)
    assert _installed_source(request.install_dir) == _OLD_SHA
    assert _probe(request.database_path) == "old"
