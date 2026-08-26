from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import build_release_manifest, write_release_manifest
from nika_core.packaging.update import (
    ReleaseIdentity,
    UpdatePhase,
    UpdateRequest,
    WindowsUpdateLifecycle,
)
from nika_core.reliability.backup import SQLiteRecoveryManager

_OLD_SOURCE_SHA = "0" * 40


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_version_one_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        statements = MIGRATIONS.get(1)
        if statements is None:
            raise RuntimeError("canonical schema no longer has migration version 1")
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (1, '2000-01-01T00:00:00+00:00')"
        )
        conn.commit()


def _create_old_install(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "NikaCore.exe").write_bytes(b"ONE-SHOT-55 deterministic old-package fixture\n")
    (path / "fixture.txt").write_text("old-package-fixture\n", encoding="utf-8")
    manifest = build_release_manifest(
        path,
        product="NikaCore",
        version="0.0.1-fixture",
        source_sha=_OLD_SOURCE_SHA,
    )
    write_release_manifest(path, manifest)


def _write_local_candidate_evidence(
    path: Path,
    *,
    artifact: Path,
    artifact_reference: str,
    source_sha: str,
) -> Path:
    payload = {
        "schema_version": 1,
        "commit_sha": source_sha,
        "distributable_zip_path": artifact_reference,
        "distributable_zip_sha256": _sha256(artifact),
        "distributable_zip_size": artifact.stat().st_size,
        "evidence_tier": "m11_exact_local_candidate",
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


class _PackagedStartupHealth:
    def __init__(self, proof_root: Path) -> None:
        self._proof_root = proof_root
        self.calls: list[str] = []

    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        executable = install_dir / "NikaCore.exe"
        if not executable.is_file():
            raise RuntimeError("installed NikaCore.exe is missing")
        output = self._proof_root / f"startup-{len(self.calls) + 1}.json"
        environment = dict(os.environ)
        environment["NIKA_DB_PATH"] = str(database_path)
        completed = subprocess.run(
            [
                str(executable),
                "--pf11-proof",
                "--pf11-proof-output",
                str(output),
            ],
            check=False,
            env=environment,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"installed packaged startup proof exited {completed.returncode}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("installed packaged startup proof emitted invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("route") != "product_project":
            raise RuntimeError("installed packaged startup proof returned invalid route")
        if payload.get("human_tested") is not False or payload.get("nvda_verified") is not False:
            raise RuntimeError("automated packaged update proof may not claim human/NVDA truth")
        self.calls.append(expected.source_sha)


def prove(
    *,
    artifact: Path,
    artifact_reference: str,
    source_sha: str,
    version: str,
    candidate_evidence: Path | None,
    output: Path,
) -> Path:
    artifact = artifact.resolve(strict=True)
    if os.name != "nt":
        raise RuntimeError("exact packaged update lifecycle proof requires Windows")

    with tempfile.TemporaryDirectory(prefix="nika-update-proof-") as temporary:
        root = Path(temporary) / "Оновлення Nika зі spaces"
        root.mkdir(parents=True)
        install = root / "installed old package"
        database = root / "durable data" / "nika.db"
        state = root / "update state"
        _create_old_install(install)
        _create_version_one_database(database)

        evidence = candidate_evidence
        evidence_tier = "m12_prehuman_candidate"
        if evidence is None:
            evidence_tier = "m11_exact_local_candidate"
            evidence = _write_local_candidate_evidence(
                root / "m11-candidate-evidence.json",
                artifact=artifact,
                artifact_reference=artifact_reference,
                source_sha=source_sha,
            )
        else:
            evidence = evidence.resolve(strict=True)

        health = _PackagedStartupHealth(root)
        request = UpdateRequest(
            artifact_path=artifact,
            evidence_path=evidence,
            artifact_reference=artifact_reference,
            install_dir=install,
            database_path=database,
            expected_product="NikaCore",
            expected_version=version,
            expected_source_sha=source_sha,
        )
        result = WindowsUpdateLifecycle(state_dir=state, health=health).run(request)
        if result.phase != UpdatePhase.COMPLETED:
            raise RuntimeError(f"packaged update proof ended in {result.phase}")
        if result.installed.source_sha != source_sha:
            raise RuntimeError("packaged update installed the wrong source SHA")
        if health.calls != [source_sha]:
            raise RuntimeError("packaged candidate did not pass one startup/health proof")
        if SQLiteStore(database).schema_version() != SCHEMA_VERSION:
            raise RuntimeError("packaged update did not migrate fixture DB to current schema")

        backup = SQLiteRecoveryManager(SQLiteStore(database)).verify_backup(
            result.pre_update_backup
        )
        rollback_manifest = json.loads(
            (result.rollback_package / "release-manifest.json").read_text(encoding="utf-8")
        )
        if rollback_manifest.get("source_sha") != _OLD_SOURCE_SHA:
            raise RuntimeError("packaged update did not retain the exact old package rollback")

        proof = {
            "schema_version": 1,
            "evidence_tier": evidence_tier,
            "candidate_source_sha": source_sha,
            "candidate_version": version,
            "candidate_zip_sha256": _sha256(artifact),
            "candidate_artifact_reference": artifact_reference,
            "operation_id": result.operation_id,
            "lifecycle_phase": result.phase.value,
            "fixture_old_package": True,
            "fixture_old_source_sha": _OLD_SOURCE_SHA,
            "fixture_old_database_schema_version": 1,
            "migrated_database_schema_version": SCHEMA_VERSION,
            "pre_update_backup_sha256": backup.sha256,
            "canonical_backup_verified": True,
            "exact_candidate_archive_reused": True,
            "packaged_startup_after_replacement_proven": True,
            "rollback_package_retained": True,
            "human_tested": False,
            "nvda_verified": False,
            "production_release_ready": False,
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-reference", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = prove(
        artifact=args.artifact,
        artifact_reference=args.artifact_reference,
        source_sha=args.source_sha.strip().lower(),
        version=args.version,
        candidate_evidence=args.candidate_evidence,
        output=args.output,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
