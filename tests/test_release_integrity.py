from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.packaging import notices
from nika_core.packaging.release import (
    DistributableEvidence,
    build_distributable_evidence,
    verify_distributable_evidence,
    write_distributable_evidence,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class _FakeMetadata(dict[str, str]):
    def get_all(self, key: str, default: list[str] | None = None) -> list[str]:
        return list(default or [])


class _FakeDistribution:
    version = "2.3.4"
    files: tuple[Path, ...] = ()
    metadata = _FakeMetadata({"Name": "demo-runtime", "License-Expression": "MIT"})


def _fake_distribution(name: str) -> _FakeDistribution:
    assert name == "demo-runtime"
    return _FakeDistribution()


def test_names_only_third_party_notices_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("demo-runtime",))
    monkeypatch.setattr(notices.metadata, "distribution", _fake_distribution)
    (tmp_path / "THIRD_PARTY_NOTICES.txt").write_text(
        "===== Python runtime =====\ndemo-runtime\n",
        encoding="utf-8",
    )

    findings = notices.verify_third_party_notices(tmp_path)

    assert findings == ("notices-version:demo-runtime",)


def test_notice_verification_requires_exact_version_license_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("demo-runtime",))
    monkeypatch.setattr(notices.metadata, "distribution", _fake_distribution)
    target = tmp_path / "THIRD_PARTY_NOTICES.txt"
    target.write_text(
        "===== Python runtime =====\n"
        "===== demo-runtime 2.3.4 =====\n"
        "Distribution provenance: demo-runtime==2.3.4\n"
        "Declared license: MIT\n",
        encoding="utf-8",
    )
    assert notices.verify_third_party_notices(tmp_path) == ()

    target.write_text(
        "===== Python runtime =====\n"
        "===== demo-runtime 2.3.4 =====\n"
        "Declared license: MIT\n",
        encoding="utf-8",
    )
    assert notices.verify_third_party_notices(tmp_path) == (
        "notices-provenance:demo-runtime",
    )

    target.write_text(
        "===== Python runtime =====\n"
        "===== demo-runtime 2.3.4 =====\n"
        "Distribution provenance: demo-runtime==2.3.4\n",
        encoding="utf-8",
    )
    assert notices.verify_third_party_notices(tmp_path) == (
        "notices-license:demo-runtime",
    )


def test_final_zip_digest_is_recorded_and_verified(tmp_path: Path) -> None:
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    artifact.write_bytes(b"final distributable bytes")

    evidence = build_distributable_evidence(artifact, source_sha=SOURCE_SHA)

    assert evidence.artifact_name == artifact.name
    assert evidence.artifact_size == artifact.stat().st_size
    assert len(evidence.artifact_sha256) == 64
    assert evidence.source_sha == SOURCE_SHA
    assert verify_distributable_evidence(
        artifact,
        evidence,
        expected_source_sha=SOURCE_SHA,
    ) == ()

    evidence_path = write_distributable_evidence(tmp_path / "release-evidence.json", evidence)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["artifact_name"] == artifact.name
    assert payload["artifact_sha256"] == evidence.artifact_sha256
    assert payload["source_sha"] == SOURCE_SHA


def test_final_zip_evidence_rejects_tampering_and_superseded_source(tmp_path: Path) -> None:
    artifact = tmp_path / "NikaCore.zip"
    artifact.write_bytes(b"candidate-a")
    evidence = build_distributable_evidence(artifact, source_sha=SOURCE_SHA)

    artifact.write_bytes(b"candidate-b")
    findings = verify_distributable_evidence(
        artifact,
        evidence,
        expected_source_sha=OTHER_SHA,
    )

    assert "source-sha" in findings
    assert "artifact-sha256" in findings


def test_final_zip_evidence_rejects_renamed_or_malformed_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"candidate")
    evidence = DistributableEvidence(
        artifact_name="superseded.zip",
        artifact_size=artifact.stat().st_size,
        artifact_sha256="not-a-digest",
        source_sha=SOURCE_SHA,
    )

    assert verify_distributable_evidence(
        artifact,
        evidence,
        expected_source_sha=SOURCE_SHA,
    ) == ("artifact-name", "artifact-sha256-format")


def test_distributable_evidence_requires_zip_and_exact_source_sha(tmp_path: Path) -> None:
    not_zip = tmp_path / "candidate.bin"
    not_zip.write_bytes(b"candidate")
    with pytest.raises(ValueError, match="final distributable ZIP"):
        build_distributable_evidence(not_zip, source_sha=SOURCE_SHA)

    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"candidate")
    with pytest.raises(ValueError, match="40-character hexadecimal commit SHA"):
        build_distributable_evidence(artifact, source_sha="deadbeef")
