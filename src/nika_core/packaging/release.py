from __future__ import annotations

import hashlib
import json
import re
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_VERSION = 2
_RELEASE_MANIFEST_NAME = "release-manifest.json"
_MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
_MANIFEST_KEYS = frozenset({"manifest_version", "product", "version", "source_sha", "files"})
_RELEASE_FILE_KEYS = frozenset({"path", "size", "sha256"})
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|?*')
_SECRET_RELEASE_BASENAMES = frozenset({".env", "token.json", "cookies.txt"})
_SECRET_CONTENT_SUFFIXES = frozenset(
    {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties"}
)
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024
_SECRET_SCAN_OVERLAP_BYTES = 8 * 1024
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\"']?
    (?:
        api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|
        secret[_-]?key|password|passwd|private[_-]?key
    )
    [\"']?
    \s*[:=]\s*
    (?P<value>
        \"(?:\\.|[^\"\\\r\n]){1,4096}\"|
        '(?:\\.|[^'\\\r\n]){1,4096}'|
        [^\s,\#;}{\]\r\n]{1,4096}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRET_PLACEHOLDER_VALUES = frozenset(
    {
        b"none",
        b"null",
        b"unset",
        b"redacted",
        b"masked",
        b"changeme",
        b"change-me",
        b"change_me",
        b"replace-me",
        b"replace_me",
        b"placeholder",
    }
)


class _DuplicateJsonKey(ValueError):
    pass


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


def _canonical_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if "\\" in value or ":" in value or value in {".", ".."}:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return False
    for part in path.parts:
        if part in {".", ".."} or part.endswith((" ", ".")):
            return False
        if any(ord(character) < 32 or character in _WINDOWS_FORBIDDEN_CHARS for character in part):
            return False
        if PureWindowsPath(part).is_reserved():
            return False
    return True


def _release_path_is_secret(value: object) -> bool:
    if not isinstance(value, str):
        return False
    for part in PurePosixPath(value).parts:
        identity = part.casefold()
        if identity in _SECRET_RELEASE_BASENAMES:
            return True
        if identity.startswith(".env.") and identity != ".env.example":
            return True
    return False


def _canonical_release_path(value: object) -> bool:
    return _canonical_relative_path(value) and value != _RELEASE_MANIFEST_NAME


def _secret_assignment_value_is_placeholder(value: bytes) -> bool:
    normalized = value.strip().strip(b"\"'").strip().lower()
    if not normalized or normalized in _SECRET_PLACEHOLDER_VALUES:
        return True
    if normalized.startswith(b"${") and normalized.endswith(b"}"):
        return True
    if normalized.startswith(b"{{") and normalized.endswith(b"}}"):
        return True
    if normalized.startswith(b"%") and normalized.endswith(b"%") and len(normalized) > 2:
        return True
    if normalized.startswith((b"env:", b"keyring:", b"credential-ref:")):
        return True
    return False


def _stream_contains_secret_assignment(handle: Any) -> bool:
    overlap = b""
    while True:
        chunk = handle.read(_SECRET_SCAN_CHUNK_BYTES)
        if not chunk:
            return False
        window = overlap + chunk
        for match in _SECRET_ASSIGNMENT_RE.finditer(window):
            if not _secret_assignment_value_is_placeholder(match.group("value")):
                return True
        overlap = window[-_SECRET_SCAN_OVERLAP_BYTES:]


def _release_file_contains_secret_assignment(relative_path: str, path: Path) -> bool:
    if PurePosixPath(relative_path).suffix.casefold() not in _SECRET_CONTENT_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            return _stream_contains_secret_assignment(handle)
    except OSError:
        return True


def _archive_member_contains_secret_assignment(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
) -> bool:
    if PurePosixPath(_zip_member_path(member)).suffix.casefold() not in _SECRET_CONTENT_SUFFIXES:
        return False
    with archive.open(member, "r") as handle:
        return _stream_contains_secret_assignment(handle)


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
    seen_windows_paths: set[str] = set()
    for index, entry in enumerate(manifest.files):
        if not isinstance(entry, ReleaseFile):
            findings.append(f"manifest:file-type:{index}")
            continue
        if not _canonical_release_path(entry.path):
            findings.append(f"manifest:path:{index}")
        elif _release_path_is_secret(entry.path):
            findings.append(f"manifest:secret-path:{entry.path}")
        elif entry.path in seen_paths:
            findings.append(f"manifest:duplicate-path:{entry.path}")
        else:
            seen_paths.add(entry.path)
            windows_identity = entry.path.casefold()
            if windows_identity in seen_windows_paths:
                findings.append(f"manifest:windows-path-collision:{entry.path}")
            else:
                seen_windows_paths.add(windows_identity)
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
        if path.name != _RELEASE_MANIFEST_NAME
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
    root = bundle_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("bundle_dir must be a directory")
    target = root / _RELEASE_MANIFEST_NAME
    payload = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
        "source_sha": manifest.source_sha,
        "files": [asdict(item) for item in manifest.files],
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".release-manifest-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            temporary_path = Path(handle.name)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
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
        if path.name != _RELEASE_MANIFEST_NAME
    }
    findings: list[str] = []
    for relative_path in sorted(actual_paths):
        if _release_path_is_secret(relative_path):
            findings.append(f"secret-path:{relative_path}")
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
            continue
        if _release_file_contains_secret_assignment(relative_path, path):
            findings.append(f"secret-content:{relative_path}")
    return tuple(findings)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _decode_json_object(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content, object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _read_evidence_object(evidence_path: Path) -> dict[str, Any] | None:
    try:
        content = evidence_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    return _decode_json_object(content)


def _decode_release_manifest(content: bytes) -> ReleaseManifest | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return None
    payload = _decode_json_object(text)
    if payload is None or frozenset(payload) != _MANIFEST_KEYS:
        return None
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        return None
    entries: list[ReleaseFile] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or frozenset(raw_entry) != _RELEASE_FILE_KEYS:
            return None
        entries.append(
            ReleaseFile(
                path=raw_entry.get("path"),
                size=raw_entry.get("size"),
                sha256=raw_entry.get("sha256"),
            )
        )
    return ReleaseManifest(
        product=payload.get("product"),
        version=payload.get("version"),
        source_sha=payload.get("source_sha"),
        files=tuple(entries),
        manifest_version=payload.get("manifest_version"),
    )


def _sha256_archive_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(member, "r") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_member_is_symlink(member: zipfile.ZipInfo) -> bool:
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    return member.create_system == 3 and stat.S_ISLNK(unix_mode)


def _zip_member_path(member: zipfile.ZipInfo) -> str:
    if member.is_dir() and member.filename.endswith("/"):
        return member.filename[:-1]
    return member.filename


def verify_release_archive(artifact_path: Path, *, source_sha: str) -> tuple[str, ...]:
    """Verify the embedded manifest against the exact files in a Windows release ZIP."""
    normalized_source_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("archive:source-sha-format",)
    if not artifact_path.is_file():
        return ("archive:missing-artifact",)

    try:
        with zipfile.ZipFile(artifact_path, "r") as archive:
            all_members = archive.infolist()
            if not all_members:
                return ("archive:empty",)

            findings: list[str] = []
            by_path: dict[str, zipfile.ZipInfo] = {}
            seen_paths: set[str] = set()
            windows_paths: set[str] = set()
            for index, member in enumerate(all_members):
                member_path = _zip_member_path(member)
                if not _canonical_relative_path(member_path):
                    findings.append(f"archive:path:{index}")
                    continue
                if member_path != _RELEASE_MANIFEST_NAME and _release_path_is_secret(member_path):
                    findings.append(f"archive:secret-path:{member_path}")
                    continue
                if _zip_member_is_symlink(member):
                    findings.append(f"archive:symlink:{index}")
                    continue
                if member_path in seen_paths:
                    if member_path == _RELEASE_MANIFEST_NAME:
                        findings.append("archive:duplicate-manifest")
                    else:
                        findings.append(f"archive:duplicate-path:{member_path}")
                    continue
                windows_identity = member_path.casefold()
                if windows_identity in windows_paths:
                    findings.append(f"archive:windows-path-collision:{member_path}")
                    continue
                seen_paths.add(member_path)
                windows_paths.add(windows_identity)
                if not member.is_dir():
                    by_path[member_path] = member
            if findings:
                return tuple(findings)
            if not by_path:
                return ("archive:empty",)

            manifest_member = by_path.get(_RELEASE_MANIFEST_NAME)
            if manifest_member is None:
                return ("archive:missing-manifest",)
            if manifest_member.file_size > _MAX_RELEASE_MANIFEST_BYTES:
                return ("archive:manifest-too-large",)
            try:
                manifest_content = archive.read(manifest_member)
            except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
                return ("archive:invalid-manifest",)
            manifest = _decode_release_manifest(manifest_content)
            if manifest is None:
                return ("archive:invalid-manifest",)
            structure_findings = _manifest_structure_findings(manifest)
            if structure_findings:
                return tuple(f"archive:{finding}" for finding in structure_findings)
            if manifest.source_sha != normalized_source_sha:
                findings.append("archive:source-sha")

            expected = {entry.path: entry for entry in manifest.files}
            actual = {
                path: member
                for path, member in by_path.items()
                if path != _RELEASE_MANIFEST_NAME
            }
            for missing in sorted(expected.keys() - actual.keys()):
                findings.append(f"archive:missing:{missing}")
            for unexpected in sorted(actual.keys() - expected.keys()):
                findings.append(f"archive:unexpected:{unexpected}")
            for relative_path in sorted(expected.keys() & actual.keys()):
                entry = expected[relative_path]
                member = actual[relative_path]
                if member.file_size != entry.size:
                    findings.append(f"archive:size:{relative_path}")
                    continue
                try:
                    actual_sha256 = _sha256_archive_member(archive, member)
                except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
                    findings.append(f"archive:unreadable:{relative_path}")
                    continue
                if actual_sha256 != entry.sha256:
                    findings.append(f"archive:sha256:{relative_path}")
                    continue
                try:
                    has_secret_content = _archive_member_contains_secret_assignment(archive, member)
                except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
                    findings.append(f"archive:unreadable:{relative_path}")
                    continue
                if has_secret_content:
                    findings.append(f"archive:secret-content:{relative_path}")
            return tuple(findings)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return ("archive:invalid-zip",)


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
