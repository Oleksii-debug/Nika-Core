from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.packaging.attestation import (
    ReleaseAttestationEvidence,
    write_release_attestation_evidence,
)


def _valid() -> ReleaseAttestationEvidence:
    return ReleaseAttestationEvidence(
        schema_version=1,
        commit_sha="a" * 40,
        artifact_reference="./dist/Nika Core-windows-x64.zip",
        artifact_sha256="b" * 64,
        artifact_size=123,
        repository="Oleksii-debug/Nika-Core",
        signer_workflow="Oleksii-debug/Nika-Core/.github/workflows/m12-prehuman-release-gate.yml",
        source_ref="refs/heads/main",
        predicate_type="https://slsa.dev/provenance/v1",
        attestation_id="42",
        attestation_url="https://github.com/Oleksii-debug/Nika-Core/attestations/42",
        verification_result_bound=True,
        human_tested=False,
        nvda_verified=False,
        production_release_ready=False,
    )


@pytest.mark.parametrize(("field", "value"), [
    ("commit_sha", "invalid"),
    ("commit_sha", True),
    ("artifact_sha256", "g" * 64),
    ("artifact_sha256", True),
    ("artifact_size", -1),
    ("artifact_size", True),
    ("artifact_size", 2**63),
    ("artifact_reference", ""),
    ("artifact_reference", "./dist/file.zip\n"),
    ("artifact_reference", "a" * 2049),
    ("repository", "other"),
    ("repository", True),
    ("signer_workflow", "other/repo/.github/workflows/m12-prehuman-release-gate.yml"),
    ("predicate_type", "https://example.invalid/predicate"),
    ("attestation_id", "0"),
    ("attestation_id", 42),
    ("attestation_url", "https://example.invalid/attestations/42"),
])
def test_invalid_identity_cannot_replace_existing_sidecar(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "m12-attestation-evidence.json"
    path.write_text("previous evidence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid provenance identity"):
        write_release_attestation_evidence(path, replace(_valid(), **{field: value}))
    assert path.read_text(encoding="utf-8") == "previous evidence\n"
    assert list(tmp_path.glob(".m12-attestation-*.tmp")) == []


def test_partial_evidence_is_refused_before_side_effect(tmp_path: Path) -> None:
    path = tmp_path / "m12-attestation-evidence.json"
    with pytest.raises(ValueError, match="invalid automated release gates"):
        write_release_attestation_evidence(path, object.__new__(ReleaseAttestationEvidence))
    assert not path.exists()


def test_unicode_directory_and_normal_provenance_still_publish(tmp_path: Path) -> None:
    path = tmp_path / "Каталог із пробілами" / "m12-attestation-evidence.json"
    write_release_attestation_evidence(path, _valid())
    assert '"attestation_id": "42"' in path.read_text(encoding="utf-8")
    assert list(path.parent.glob(".m12-attestation-*.tmp")) == []
