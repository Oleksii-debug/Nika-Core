from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_VERSION = 2


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
    manifest_version: int = _MANIFEST_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _canonical_release_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if "\\" in value or ":" in value or value in {".", ".."}:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return False
    if any(part in {".", ".."} for part in path.parts):
        return False
    return value != "release-manifest.json"


def _manifest_structure_findings(manifest: ReleaseManifest) -> tuple[str, ...]:
    findings: list[str] = []
    if type(manifest.manifest_version) is not int or manifest.manifest_version != _MANIFEST_VERSION:
        findings.append("manifest:schema-version")
    if (
        not isinstance(manifest.product, str)
        or not manifest.product
        or manifest.product != manifest.product.strip()
    ):
        findings.append("manifest:product")
    if (
        not isinstance(manifest.version, str)
        or not manifest.version
        or manifest.version != manifest.version.strip()
    ):
        findings.append("manifest:product-version")
    if (
        not isinstance(manifest.source_sha, str)
        or not _SOURCE_SHA_RE.fullmatch(manifest.source_sha)
    ):
        findings.append("manifest:source-sha")
    if not isinstance(manifest.files, tuple) or not manifest.files:
        findings.append("manifest:files")
        return tuple(findings)

    seen_paths: set[str] = set()
    for index, entry in enumerate(manifest.files):
        if not isinstance(entry, ReleaseFile):
            findings.append(f"manifest:file-type:{index}")
            continue
        if not _canonical_release_path(entry.path):
            findings.append(f"manifest:path:{index}")
        elif entry.path in seen_paths:
            findings.append(f"manifest:duplicate-path:{entry.path}")
        else:
            seen_paths.add(entry.path)
        if type(entry.size) is not int or entry.size < 0:
            findings.append(f"manifest:size-format:{index}")
        if not isinstance(entry.sha256, str) or not _SHA256_RE.fullmatch(entry.sha256):
            findings.append(f"manifest:sha256-format:{index}")
    return tuple(findings)


def _require_valid_manifest(manifest: ReleaseManifest) -> None:
    findings = _manifest_structure_findings(manifest)
    if findings:
        raise ValueError(f"invalid release manifest: {findings}")


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
    manifest = ReleaseManifest(
        product=product,
        version=version,
        source_sha=source_sha,
        files=entries,
    )
    _require_valid_manifest(manifest)
    return manifest


def write_release_manifest(bundle_dir: Path, manifest: ReleaseManifest) -> Path:
    _require_valid_manifest(manifest)
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
    structure_findings = _manifest_structure_findings(manifest)
    if structure_findings:
        return structure_findings

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


def _read_evidence_object(evidence_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def verify_distributable_evidence(
    artifact_path: Path,
    evidence_path: Path,
    *,
    source_sha: str,
    artifact_reference: str,
) -> tuple[str, ...]:
    """Verify that pre-human evidence binds the exact uploaded distributable.

    The evidence is intentionally outside the ZIP: embedding its own digest would be
    recursive. The verifier therefore binds an immutable outer artifact by path,
    byte size, SHA-256, and exact source commit immediately before upload.
    """
    findings: list[str] = []
    normalized_source_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("distributable:source-sha-format",)
    if not artifact_path.is_file():
        return ("distributable:missing-artifact",)

    payload = _read_evidence_object(evidence_path)
    if payload is None:
        return ("distributable:invalid-evidence",)

    if payload.get("commit_sha") != normalized_source_sha:
        findings.append("distributable:source-sha")
    if payload.get("distributable_zip_path") != artifact_reference:
        findings.append("distributable:path")

    expected_size = payload.get("distributable_zip_size")
    if type(expected_size) is not int or expected_size < 0:
        findings.append("distributable:size-format")
    elif artifact_path.stat().st_size != expected_size:
        findings.append("distributable:size")

    expected_sha256 = payload.get("distributable_zip_sha256")
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        findings.append("distributable:sha256-format")
    elif _sha256(artifact_path) != expected_sha256:
        findings.append("distributable:sha256")
    return tuple(findings)
