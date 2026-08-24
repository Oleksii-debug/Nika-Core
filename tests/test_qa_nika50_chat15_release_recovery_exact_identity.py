from __future__ import annotations

import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.recovery import ReleaseDatabaseRecovery
from nika_core.product_factory_deployment import ReleaseRef
from nika_core.reliability.backup import SQLiteRecoveryManager

SNAPSHOT_SHA = "1" * 40
CURRENT_SHA = "2" * 40


def _recovery(database: Path) -> ReleaseDatabaseRecovery:
    store = SQLiteStore(database)
    store.initialize()
    return ReleaseDatabaseRecovery(SQLiteRecoveryManager(store))


def _release(*, version: str, source_sha: str, digest: str) -> ReleaseRef:
    return ReleaseRef(
        project_id="project-release-recovery",
        version=version,
        source_sha=source_sha,
        artifact_digest=digest,
    )


def test_snapshot_metadata_binds_full_release_identity(tmp_path: Path) -> None:
    recovery = _recovery(tmp_path / "nika.db")
    source_release = _release(
        version="1.0.0",
        source_sha=SNAPSHOT_SHA,
        digest="a" * 64,
    )
    snapshot_dir = tmp_path / "release snapshot"

    recovery.create_snapshot(
        snapshot_dir,
        source_release_sha=source_release.source_sha,
    )

    metadata = json.loads(
        (snapshot_dir / "release-database-snapshot.json").read_text(encoding="utf-8")
    )
    assert source_release.version in metadata.values()
    assert source_release.artifact_digest in metadata.values()


def test_restore_confirmation_distinguishes_same_sha_exact_releases(tmp_path: Path) -> None:
    recovery = _recovery(tmp_path / "nika.db")
    source_release = _release(
        version="1.0.0",
        source_sha=SNAPSHOT_SHA,
        digest="a" * 64,
    )
    current_a = _release(
        version="2.0.0",
        source_sha=CURRENT_SHA,
        digest="b" * 64,
    )
    current_b = _release(
        version="2.0.1",
        source_sha=CURRENT_SHA,
        digest="c" * 64,
    )
    assert current_a != current_b

    snapshot_dir = tmp_path / "release snapshot"
    recovery.create_snapshot(
        snapshot_dir,
        source_release_sha=source_release.source_sha,
    )

    plan_a = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=source_release.source_sha,
        current_release_sha=current_a.source_sha,
    )
    plan_b = recovery.prepare_restore(
        snapshot_dir,
        expected_source_release_sha=source_release.source_sha,
        current_release_sha=current_b.source_sha,
    )

    assert plan_a.confirmation_fingerprint != plan_b.confirmation_fingerprint
