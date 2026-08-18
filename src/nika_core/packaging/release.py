from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    product: str
    version: str
    files: tuple[ReleaseFile, ...]
    manifest_version: int = 1


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


def build_release_manifest(bundle_dir: Path, *, product: str, version: str) -> ReleaseManifest:
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
    return ReleaseManifest(product=product, version=version, files=entries)


def write_release_manifest(bundle_dir: Path, manifest: ReleaseManifest) -> Path:
    target = bundle_dir / "release-manifest.json"
    payload = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
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
