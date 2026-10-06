from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.packaging.release import (
    _bounded_json_depth,
    _read_stable_release_bytes,
    _stable_release_file_identity,
    verify_distributable_evidence,
)

_SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ATTESTATION_ID_RE = re.compile(r"^[1-9][0-9]*$")
_MAX_ATTESTATION_VERIFICATION_BYTES = 8 * 1024 * 1024


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


def _read_verification(path: Path) -> list[dict[str, Any]]:
    content = _read_stable_release_bytes(
        path,
        max_bytes=_MAX_ATTESTATION_VERIFICATION_BYTES,
    )
    if content is None:
        raise ValueError(
            "attestation verification output is unreadable, unstable, or exceeds the size limit"
        )
    if not _bounded_json_depth(content):
        raise ValueError("attestation verification output exceeds structural limits")
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
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
    expected_product_version: str,
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
        expected_product_version=expected_product_version,
    )
    if distributable_findings:
        raise ValueError(
            "pre-human distributable evidence mismatch: " + ", ".join(distributable_findings)
        )

    artifact_identity = _stable_release_file_identity(artifact_path)
    if artifact_identity is None:
        raise ValueError("attestation artifact changed during evidence binding")
    artifact_size, artifact_sha256 = artifact_identity
    if not _SHA256_RE.fullmatch(artifact_sha256):
        raise ValueError("artifact SHA-256 calculation failed")

    verification = _read_verification(verification_path)
    if not _has_matching_slsa_subject(verification, artifact_sha256=artifact_sha256):
        raise ValueError(
            "verified attestation output does not contain SLSA provenance for exact artifact digest"
        )

    # Bind provenance to the same distributable contract before and after the
    # attestation verifier output is consumed. Every artifact observation uses
    # the canonical held-descriptor snapshot authority, including Windows's
    # write/delete sharing fence.
    refreshed_findings = verify_distributable_evidence(
        artifact_path,
        prehuman_evidence_path,
        source_sha=normalized_source_sha,
        artifact_reference=artifact_reference,
        expected_product_version=expected_product_version,
    )
    if refreshed_findings:
        raise ValueError(
            "pre-human distributable evidence changed during attestation: "
            + ", ".join(refreshed_findings)
        )
    if _stable_release_file_identity(artifact_path) != artifact_identity:
        raise ValueError("attestation artifact changed during evidence binding")

    return ReleaseAttestationEvidence(
        schema_version=1,
        commit_sha=normalized_source_sha,
        artifact_reference=artifact_reference,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
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
    if type(evidence) is not ReleaseAttestationEvidence:
        raise ValueError("attestation evidence has invalid provenance identity")
    payload = {
        name: getattr(evidence, name, None)
        for name in ReleaseAttestationEvidence.__dataclass_fields__
    }
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["verification_result_bound"] is not True
        or payload["source_ref"] != "refs/heads/main"
        or payload["human_tested"] is not False
        or payload["nvda_verified"] is not False
        or payload["production_release_ready"] is not False
    ):
        raise ValueError("attestation evidence has invalid automated release gates")

    repository = payload["repository"]
    attestation_id = payload["attestation_id"]
    artifact_reference = payload["artifact_reference"]
    if (
        not isinstance(payload["commit_sha"], str)
        or not _SOURCE_SHA_RE.fullmatch(payload["commit_sha"])
        or not isinstance(payload["artifact_sha256"], str)
        or not _SHA256_RE.fullmatch(payload["artifact_sha256"])
        or type(payload["artifact_size"]) is not int
        or not 0 < payload["artifact_size"] <= 2**63 - 1
        or not isinstance(artifact_reference, str)
        or not artifact_reference
        or artifact_reference != artifact_reference.strip()
        or not artifact_reference.isprintable()
        or len(artifact_reference.encode("utf-8")) > 2048
        or not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or payload["signer_workflow"]
        != f"{repository}/.github/workflows/m12-prehuman-release-gate.yml"
        or payload["predicate_type"] != _SLSA_PROVENANCE_V1
        or not isinstance(attestation_id, str)
        or not _ATTESTATION_ID_RE.fullmatch(attestation_id)
        or payload["attestation_url"]
        != f"https://github.com/{repository}/attestations/{attestation_id}"
    ):
        raise ValueError("attestation evidence has invalid provenance identity")

    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".m12-attestation-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
