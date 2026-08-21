from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    product: str
    version: str
    source_sha: str
    files: tuple[ReleaseFile, ...]
    manifest_version: int = 2


@dataclass(frozen=True, slots=True)
class DistributableEvidence:
    artifact_name: str
    artifact_size: int
    artifact_sha256: str
    source_sha: str
    evidence_version: int = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_source_sha(source_sha: str) -> str:
    normalized = source_sha.strip().lower()
    if not _SOURCE_SHA.fullmatch(normalized):
        raise ValueError("source_sha must be an exact 40-character hexadecimal commit SHA")
    return normalized


def _safe_files(bundle_dir: Path) -> tuple[Path, ...]:
    root = bundle_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("bundle_dir must be a directory")
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"bundle symlink escapes release root: {candidate}") from exc
        if candidate.is_file():
            files.append(candidate)
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def build_release_manifest(
    bundle_dir: Path,
    *,
    product: str,
    version: str,
    source_sha: str,
) -> ReleaseManifest:
    root = bundle_dir.resolve(strict=True)
    entries = tuple(
        ReleaseFile(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            sha256=_sha256(path),
        )
        for path in _safe_files(root)
        if path.name != "release-manifest.json"
    )
    if not entries:
        raise ValueError("release bundle is empty")
    return ReleaseManifest(
        product=product,
        version=version,
        source_sha=source_sha,
        files=entries,
    )


def write_release_manifest(bundle_dir: Path, manifest: ReleaseManifest) -> Path:
    target = bundle_dir / "release-manifest.json"
    payload = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
        "source_sha": manifest.source_sha,
        "files": [asdict(item) for item in manifest.files],
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def verify_release_manifest(bundle_dir: Path, manifest: ReleaseManifest) -> tuple[str, ...]:
    root = bundle_dir.resolve(strict=True)
    expected = {entry.path: entry for entry in manifest.files}
    actual_paths = {
        path.relative_to(root).as_posix(): path
        for path in _safe_files(root)
        if path.name != "release-manifest.json"
    }
    findings: list[str] = []
    for missing in sorted(expected.keys() - actual_paths.keys()):
        findings.append(f"missing:{missing}")
    for unexpected in sorted(actual_paths.keys() - expected.keys()):
        findings.append(f"unexpected:{unexpected}")
    for relative_path in sorted(expected.keys() & actual_paths.keys()):
        entry = expected[relative_path]
        path = actual_paths[relative_path]
        if path.stat().st_size != entry.size:
            findings.append(f"size:{relative_path}")
            continue
        if _sha256(path) != entry.sha256:
            findings.append(f"sha256:{relative_path}")
    return tuple(findings)


def build_distributable_evidence(artifact: Path, *, source_sha: str) -> DistributableEvidence:
    """Bind release evidence to the final ZIP bytes and exact source commit."""

    candidate = artifact.resolve(strict=True)
    if not candidate.is_file():
        raise ValueError("release artifact must be a file")
    if candidate.suffix.casefold() != ".zip":
        raise ValueError("release artifact must be the final distributable ZIP")
    return DistributableEvidence(
        artifact_name=candidate.name,
        artifact_size=candidate.stat().st_size,
        artifact_sha256=_sha256(candidate),
        source_sha=_validated_source_sha(source_sha),
    )


def write_distributable_evidence(target: Path, evidence: DistributableEvidence) -> Path:
    if not _SHA256.fullmatch(evidence.artifact_sha256):
        raise ValueError("artifact_sha256 must be an exact lowercase SHA-256 digest")
    payload = asdict(evidence)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def verify_distributable_evidence(
    artifact: Path,
    evidence: DistributableEvidence,
    *,
    expected_source_sha: str,
) -> tuple[str, ...]:
    """Reject stale, superseded, renamed or tampered distributable evidence fail-closed."""

    candidate = artifact.resolve(strict=True)
    findings: list[str] = []
    expected_source = _validated_source_sha(expected_source_sha)
    if evidence.evidence_version != 1:
        findings.append("evidence-version")
    if evidence.source_sha != expected_source:
        findings.append("source-sha")
    if evidence.artifact_name != candidate.name:
        findings.append("artifact-name")
    if evidence.artifact_size != candidate.stat().st_size:
        findings.append("artifact-size")
    if not _SHA256.fullmatch(evidence.artifact_sha256):
        findings.append("artifact-sha256-format")
    elif evidence.artifact_sha256 != _sha256(candidate):
        findings.append("artifact-sha256")
    return tuple(findings)
