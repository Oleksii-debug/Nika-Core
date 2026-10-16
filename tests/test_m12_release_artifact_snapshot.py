from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.m12_release_evidence as release_evidence
from scripts.m12_release_evidence import _snapshot_release_artifact


def test_release_artifact_snapshot_preserves_verified_bytes_after_source_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"verified bytes")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    snapshot = _snapshot_release_artifact(source, snapshot_dir)
    source.write_bytes(b"replacement bytes")

    assert snapshot.read_bytes() == b"verified bytes"
    assert source.read_bytes() == b"replacement bytes"


def test_release_artifact_snapshot_rejects_symlink_source(tmp_path: Path) -> None:
    target = tmp_path / "target.zip"
    target.write_bytes(b"target")
    source = tmp_path / "candidate.zip"
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    with pytest.raises(RuntimeError, match="missing or unsafe"):
        _snapshot_release_artifact(source, snapshot_dir)


def test_release_artifact_snapshot_rejects_lstat_open_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"original")
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(b"attacker")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    real_open = release_evidence.os.open
    swapped = False

    def swapping_open(path: os.PathLike[str] | str, flags: int, *args: object) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source:
            os.replace(replacement, source)
            swapped = True
        return real_open(path, flags, *args)

    monkeypatch.setattr(release_evidence.os, "open", swapping_open)

    with pytest.raises(RuntimeError, match="changed before snapshot"):
        _snapshot_release_artifact(source, snapshot_dir)

    assert swapped
    assert not (snapshot_dir / "verified-distributable.zip").exists()


def test_main_binds_all_release_checks_to_one_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        artifact=artifact,
        evidence=evidence,
        source_sha="0123456789abcdef0123456789abcdef01234567",
        artifact_reference="./dist/NikaCore.zip",
        product_version="0.0.2",
    )

    class FakeParser:
        def parse_args(self) -> SimpleNamespace:
            return args

    snapshots: list[Path] = []
    verified: list[Path] = []
    archived: list[Path] = []
    lifecycle: list[Path] = []
    real_snapshot = release_evidence._snapshot_release_artifact

    def snapshot_wrapper(source: Path, snapshot_dir: Path) -> Path:
        result = real_snapshot(source, snapshot_dir)
        snapshots.append(result)
        return result

    def verify_outer(path: Path, *_args: object, **_kwargs: object) -> tuple[str, ...]:
        verified.append(path)
        return ()

    def verify_archive(path: Path, **_kwargs: object) -> tuple[str, ...]:
        archived.append(path)
        return ()

    monkeypatch.setattr(release_evidence, "parser", lambda: FakeParser())
    monkeypatch.setattr(release_evidence, "_snapshot_release_artifact", snapshot_wrapper)
    monkeypatch.setattr(release_evidence, "verify_distributable_evidence", verify_outer)
    monkeypatch.setattr(release_evidence, "verify_release_archive", verify_archive)
    monkeypatch.setattr(
        release_evidence,
        "prove_packaged_installer_lifecycle",
        lambda path: lifecycle.append(path),
    )

    assert release_evidence.main() == 0

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot != artifact
    assert verified == [snapshot]
    assert archived == [snapshot]
    if os.name == "nt":
        assert lifecycle == [snapshot]
    else:
        assert lifecycle == []
