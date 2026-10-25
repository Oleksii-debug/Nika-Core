from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.packaging.attestation import (
    ReleaseAttestationEvidence,
    write_release_attestation_evidence,
)


def _evidence() -> ReleaseAttestationEvidence:
    return ReleaseAttestationEvidence(
        schema_version=1,
        commit_sha="a" * 40,
        artifact_reference="./dist/NikaCore-windows-x64.zip",
        artifact_sha256="b" * 64,
        artifact_size=123,
        repository="Oleksii-debug/Nika-Core",
        signer_workflow=(
            "Oleksii-debug/Nika-Core/.github/workflows/m12-prehuman-release-gate.yml"
        ),
        source_ref="refs/heads/main",
        predicate_type="https://slsa.dev/provenance/v1",
        attestation_id="42",
        attestation_url="https://github.com/Oleksii-debug/Nika-Core/attestations/42",
        verification_result_bound=True,
        human_tested=False,
        nvda_verified=False,
        production_release_ready=False,
    )


def test_atomic_sidecar_round_trip_with_unicode_windows_directory(tmp_path: Path) -> None:
    directory = tmp_path / "Реліз із пробілами"
    target = directory / "m12-attestation-evidence.json"

    write_release_attestation_evidence(target, _evidence())

    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["verification_result_bound"] is True
    assert stored["human_tested"] is False
    assert stored["nvda_verified"] is False
    assert stored["production_release_ready"] is False
    assert list(directory.glob(".m12-attestation-*.tmp")) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("human_tested", True),
        ("human_tested", 0),
        ("nvda_verified", True),
        ("nvda_verified", "false"),
        ("production_release_ready", True),
        ("production_release_ready", 1),
        ("verification_result_bound", False),
        ("verification_result_bound", 1),
        ("schema_version", 2),
        ("schema_version", True),
        ("source_ref", "refs/pull/42/merge"),
    ],
)
def test_untrusted_evidence_cannot_change_automated_release_gates(
    tmp_path: Path, field: str, value: object
) -> None:
    target = tmp_path / "m12-attestation-evidence.json"
    target.write_text("previous verified evidence\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid automated release gates"):
        write_release_attestation_evidence(target, replace(_evidence(), **{field: value}))

    assert target.read_text(encoding="utf-8") == "previous verified evidence\n"
    assert list(tmp_path.glob(".m12-attestation-*.tmp")) == []


@pytest.mark.parametrize("existing", [False, True])
def test_failed_atomic_promotion_preserves_previous_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    target = tmp_path / "m12-attestation-evidence.json"
    if existing:
        target.write_text("previous verified evidence\n", encoding="utf-8")

    def fail_replace(self: Path, destination: Path) -> Path:
        assert self.name.startswith(".m12-attestation-")
        assert destination == target
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted publication"):
        write_release_attestation_evidence(target, _evidence())

    assert target.exists() is existing
    if existing:
        assert target.read_text(encoding="utf-8") == "previous verified evidence\n"
    assert list(tmp_path.glob(".m12-attestation-*.tmp")) == []


def test_failed_file_sync_leaves_existing_evidence_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "m12-attestation-evidence.json"
    target.write_text("previous verified evidence\n", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("simulated file sync failure")

    monkeypatch.setattr("nika_core.packaging.attestation.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="file sync failure"):
        write_release_attestation_evidence(target, _evidence())

    assert target.read_text(encoding="utf-8") == "previous verified evidence\n"
    assert list(tmp_path.glob(".m12-attestation-*.tmp")) == []
