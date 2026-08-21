from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.packaging.attestation import (
    build_release_attestation_evidence,
    write_release_attestation_evidence,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "Oleksii-debug/Nika-Core"
SIGNER = f"{REPOSITORY}/.github/workflows/m12-prehuman-release-gate.yml"
SOURCE_REF = "refs/heads/main"
ARTIFACT_REFERENCE = "./dist/NikaCore-1.0.0-windows-x64.zip"
ATTESTATION_ID = "123456"
ATTESTATION_URL = f"https://github.com/{REPOSITORY}/attestations/{ATTESTATION_ID}"


def _artifact(tmp_path: Path) -> Path:
    path = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    path.write_bytes(b"exact-final-distributable")
    return path


def _prehuman_evidence(tmp_path: Path, artifact: Path) -> Path:
    path = tmp_path / "m12-prehuman-evidence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "commit_sha": SOURCE_SHA,
                "distributable_zip_path": ARTIFACT_REFERENCE,
                "distributable_zip_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "distributable_zip_size": artifact.stat().st_size,
                "human_tested": False,
                "nvda_verified": False,
                "production_release_ready": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def _verification(tmp_path: Path, artifact: Path, *, digest: str | None = None) -> Path:
    path = tmp_path / "verification.json"
    path.write_text(
        json.dumps(
            [
                {
                    "attestation": {"bundle": "verified-by-gh-cli"},
                    "verificationResult": {
                        "statement": {
                            "predicateType": "https://slsa.dev/provenance/v1",
                            "subject": [
                                {
                                    "name": artifact.name,
                                    "digest": {
                                        "sha256": digest
                                        or hashlib.sha256(artifact.read_bytes()).hexdigest()
                                    },
                                }
                            ],
                        },
                        "signature": {"certificate": {"issuer": "github-actions"}},
                        "verifiedTimestamps": [{"type": "rekor"}],
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _build(tmp_path: Path):
    artifact = _artifact(tmp_path)
    prehuman = _prehuman_evidence(tmp_path, artifact)
    verification = _verification(tmp_path, artifact)
    return artifact, prehuman, verification


def test_exact_verified_attestation_builds_non_human_sidecar(tmp_path: Path) -> None:
    artifact, prehuman, verification = _build(tmp_path)

    evidence = build_release_attestation_evidence(
        artifact,
        prehuman,
        verification,
        source_sha=SOURCE_SHA,
        artifact_reference=ARTIFACT_REFERENCE,
        repository=REPOSITORY,
        signer_workflow=SIGNER,
        source_ref=SOURCE_REF,
        attestation_id=ATTESTATION_ID,
        attestation_url=ATTESTATION_URL,
    )

    assert evidence.commit_sha == SOURCE_SHA
    assert evidence.artifact_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert evidence.artifact_size == artifact.stat().st_size
    assert evidence.cryptographic_verification_completed is True
    assert evidence.human_tested is False
    assert evidence.nvda_verified is False
    assert evidence.production_release_ready is False

    output = tmp_path / "m12-attestation-evidence.json"
    write_release_attestation_evidence(output, evidence)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["attestation_id"] == ATTESTATION_ID
    assert payload["predicate_type"] == "https://slsa.dev/provenance/v1"
    assert payload["human_tested"] is False


def test_attestation_rejects_tampered_distributable_before_crypto_claim(tmp_path: Path) -> None:
    artifact, prehuman, verification = _build(tmp_path)
    artifact.write_bytes(b"tampered-after-prehuman-evidence")

    with pytest.raises(ValueError, match="pre-human distributable evidence mismatch"):
        build_release_attestation_evidence(
            artifact,
            prehuman,
            verification,
            source_sha=SOURCE_SHA,
            artifact_reference=ARTIFACT_REFERENCE,
            repository=REPOSITORY,
            signer_workflow=SIGNER,
            source_ref=SOURCE_REF,
            attestation_id=ATTESTATION_ID,
            attestation_url=ATTESTATION_URL,
        )


def test_attestation_rejects_verified_result_for_other_digest(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    prehuman = _prehuman_evidence(tmp_path, artifact)
    verification = _verification(tmp_path, artifact, digest="f" * 64)

    with pytest.raises(ValueError, match="exact artifact digest"):
        build_release_attestation_evidence(
            artifact,
            prehuman,
            verification,
            source_sha=SOURCE_SHA,
            artifact_reference=ARTIFACT_REFERENCE,
            repository=REPOSITORY,
            signer_workflow=SIGNER,
            source_ref=SOURCE_REF,
            attestation_id=ATTESTATION_ID,
            attestation_url=ATTESTATION_URL,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_ref", "refs/pull/99/merge", "restricted to integrated main"),
        ("signer_workflow", "attacker/repo/.github/workflows/build.yml", "canonical M12 workflow"),
        ("attestation_id", "0", "positive decimal"),
        ("attestation_url", "https://example.invalid/attestations/123456", "does not match"),
    ],
)
def test_attestation_identity_policy_fails_closed(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    artifact, prehuman, verification = _build(tmp_path)
    kwargs = {
        "source_sha": SOURCE_SHA,
        "artifact_reference": ARTIFACT_REFERENCE,
        "repository": REPOSITORY,
        "signer_workflow": SIGNER,
        "source_ref": SOURCE_REF,
        "attestation_id": ATTESTATION_ID,
        "attestation_url": ATTESTATION_URL,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        build_release_attestation_evidence(
            artifact,
            prehuman,
            verification,
            **kwargs,
        )


def test_attestation_verification_output_must_be_nonempty_json_array(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    prehuman = _prehuman_evidence(tmp_path, artifact)
    verification = tmp_path / "verification.json"
    verification.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="at least one result"):
        build_release_attestation_evidence(
            artifact,
            prehuman,
            verification,
            source_sha=SOURCE_SHA,
            artifact_reference=ARTIFACT_REFERENCE,
            repository=REPOSITORY,
            signer_workflow=SIGNER,
            source_ref=SOURCE_REF,
            attestation_id=ATTESTATION_ID,
            attestation_url=ATTESTATION_URL,
        )


def test_m12_workflow_keeps_signing_privilege_on_trusted_main_only() -> None:
    workflow = Path(".github/workflows/m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )

    assert "attest-main-distributable:" in workflow
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "artifact-metadata: write" in workflow
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in workflow
    assert (
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
        in workflow
    )
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "--signer-workflow" in workflow
    assert "--source-digest '${{ github.sha }}'" in workflow
    assert "--source-ref '${{ github.ref }}'" in workflow
    assert "--deny-self-hosted-runners" in workflow
