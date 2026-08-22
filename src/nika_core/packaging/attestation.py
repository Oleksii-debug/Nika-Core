from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nika_core.packaging.release import verify_distributable_evidence

_SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ATTESTATION_ID_RE = re.compile(r"^[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class ReleaseAttestationEvidence:
    schema_version: int
    commit_sha: str
    artifact_reference: str
    artifact_sha256: str
    artifact_size: int
    repository: str
    signer_workflow: str
    source_ref: str
    predicate_type: str
    attestation_id: str
    attestation_url: str
    verification_result_bound: bool
    human_tested: bool
    nvda_verified: bool
    production_release_ready: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_verification(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("attestation verification output is invalid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("attestation verification output must contain at least one result")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("attestation verification result entries must be objects")
    return payload


def _has_matching_slsa_subject(
    verification: list[dict[str, Any]],
    *,
    artifact_sha256: str,
) -> bool:
    for entry in verification:
        result = entry.get("verificationResult")
        if not isinstance(result, dict):
            continue
        statement = result.get("statement")
        if not isinstance(statement, dict):
            continue
        if statement.get("predicateType") != _SLSA_PROVENANCE_V1:
            continue
        subjects = statement.get("subject")
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            if not isinstance(subject, dict):
                continue
            digest = subject.get("digest")
            if isinstance(digest, dict) and digest.get("sha256") == artifact_sha256:
                return True
    return False


def build_release_attestation_evidence(
    artifact_path: Path,
    prehuman_evidence_path: Path,
    verification_path: Path,
    *,
    source_sha: str,
    artifact_reference: str,
    repository: str,
    signer_workflow: str,
    source_ref: str,
    attestation_id: str,
    attestation_url: str,
) -> ReleaseAttestationEvidence:
    normalized_source_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        raise ValueError("attestation source SHA must be a lowercase 40-character hex digest")
    if not artifact_path.is_file():
        raise ValueError("attestation artifact does not exist")
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError("attestation repository must use owner/repository form")
    expected_signer = f"{repository}/.github/workflows/m12-prehuman-release-gate.yml"
    if signer_workflow != expected_signer:
        raise ValueError("attestation signer workflow is not the canonical M12 workflow")
    if source_ref != "refs/heads/main":
        raise ValueError("cryptographic release attestation is restricted to integrated main")
    if not _ATTESTATION_ID_RE.fullmatch(attestation_id):
        raise ValueError("attestation id must be a positive decimal identifier")
    expected_url = f"https://github.com/{repository}/attestations/{attestation_id}"
    if attestation_url != expected_url:
        raise ValueError("attestation URL does not match repository and attestation id")

    distributable_findings = verify_distributable_evidence(
        artifact_path,
        prehuman_evidence_path,
        source_sha=normalized_source_sha,
        artifact_reference=artifact_reference,
    )
    if distributable_findings:
        raise ValueError(
            "pre-human distributable evidence mismatch: " + ", ".join(distributable_findings)
        )

    artifact_sha256 = _sha256(artifact_path)
    if not _SHA256_RE.fullmatch(artifact_sha256):
        raise ValueError("artifact SHA-256 calculation failed")

    verification = _read_verification(verification_path)
    if not _has_matching_slsa_subject(verification, artifact_sha256=artifact_sha256):
        raise ValueError(
            "verified attestation output does not contain SLSA provenance for exact artifact digest"
        )

    return ReleaseAttestationEvidence(
        schema_version=1,
        commit_sha=normalized_source_sha,
        artifact_reference=artifact_reference,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_path.stat().st_size,
        repository=repository,
        signer_workflow=signer_workflow,
        source_ref=source_ref,
        predicate_type=_SLSA_PROVENANCE_V1,
        attestation_id=attestation_id,
        attestation_url=attestation_url,
        verification_result_bound=True,
        human_tested=False,
        nvda_verified=False,
        production_release_ready=False,
    )


def write_release_attestation_evidence(
    path: Path,
    evidence: ReleaseAttestationEvidence,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(evidence), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
