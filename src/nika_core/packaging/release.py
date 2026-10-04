from __future__ import annotations

import hashlib
import json
import lzma
import re
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_VERSION = 2
_RELEASE_MANIFEST_NAME = "release-manifest.json"
_MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_PRODUCT_VERSION_CHARS = 128
_MANIFEST_KEYS = frozenset({"manifest_version", "product", "version", "source_sha", "files"})
_RELEASE_FILE_KEYS = frozenset({"path", "size", "sha256"})
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|?*')
_MAX_WINDOWS_COMPONENT_UTF16_UNITS = 255
_UNSAFE_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_SECRET_RELEASE_BASENAMES = frozenset({".env", "token.json", "cookies.txt"})
_SECRET_CONTENT_SUFFIXES = frozenset(
    {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties", ".txt", ".log"}
)
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024
_SECRET_SCAN_OVERLAP_BYTES = 8 * 1024
_PREHUMAN_EVIDENCE_SCHEMA_VERSION = 3
_PREHUMAN_REQUIRED_TRUE_FIELDS = (
    "release_manifest_source_sha_bound",
    "exact_checkout_sha_verified",
    "core_ci_equivalent",
    "full_test_suite",
    "runtime_restart_recovery",
    "memory_scheduler_resource_regressions",
    "model_mock_nollm_regressions",
    "deterministic_brain_regressions",
    "foundry_local_adapter_regressions",
    "plugin_workspace_regressions",
    "security_sandbox_regressions",
    "integrated_ubuntu",
    "integrated_windows",
    "browser_semantic_proof",
    "windows_uia_semantic_proof",
    "windows_package_built",
    "manifest_verified",
    "third_party_notices_verified",
    "packaged_uia_keyboard_focus",
)
_PREHUMAN_REQUIRED_FALSE_FIELDS = (
    "physical_windows_foundry_inference_proven",
    "human_tested",
    "nvda_verified",
    "production_release_ready",
)
_PREHUMAN_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "product_version",
        "commit_sha",
        "distributable_zip_path",
        "distributable_zip_sha256",
        "distributable_zip_size",
        *_PREHUMAN_REQUIRED_TRUE_FIELDS,
        *_PREHUMAN_REQUIRED_FALSE_FIELDS,
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\r\n{,\[]
    (?:\xef\xbb\xbf)?
    [ \t-]*
    (?P<quote>["'])?
    (?:
        api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|
        secret[_-]?key|password|passwd|private[_-]?key
    )
    (?(quote)(?P=quote))
    \s*[:=]\s*
    (?P<value>
        "(?:\\.|[^"\\\r\n]){1,4096}"|
        '(?:\\.|[^'\\\r\n]){1,4096}'|
        # Unquoted env-variable references are placeholders only when complete.
        \$\{[A-Za-z_][A-Za-z0-9_]*\}(?=[\s,;\#}\]\r\n]|$)|
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
    directories = [root]
    while directories:
        # Path.rglob can suppress nested directory scanning errors. Explicit
        # iteration must fail rather than certify a silently incomplete release.
        for candidate in directories.pop().iterdir():
            # ZIP publication cannot materialize directory aliases. Fail closed.
            if getattr(candidate, "is_junction", lambda: False)():
                raise ValueError(f"bundle junction is unsupported: {candidate}")
            if candidate.is_symlink():
                resolved = candidate.resolve(strict=True)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError(
                        f"bundle symlink escapes release root: {candidate}"
                    ) from exc
                if candidate.is_dir():
                    raise ValueError(
                        f"bundle directory symlink is unsupported: {candidate}"
                    )
            if candidate.is_file():
                files.append(candidate)
            elif candidate.is_dir():
                directories.append(candidate)
            else:
                # Never silently omit FIFO, socket, device or special-file links.
                # Opening a FIFO could also block the release worker indefinitely.
                raise ValueError(f"unsupported release bundle entry: {candidate}")
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
        if any(
            character in _WINDOWS_FORBIDDEN_CHARS
            or unicodedata.category(character) in _UNSAFE_UNICODE_CATEGORIES
            for character in part
        ):
            return False
        # NTFS counts UTF-16 code units, not Python Unicode code points.
        # Extended-length path support never lifts the per-component limit.
        component_units = sum(2 if ord(char) > 0xFFFF else 1 for char in part)
        if component_units > _MAX_WINDOWS_COMPONENT_UTF16_UNITS:
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
    return _canonical_relative_path(value) and value.casefold() != _RELEASE_MANIFEST_NAME


def _release_file_directory_collisions(
    file_paths: tuple[str, ...], directory_paths: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Reject file ancestors of files/directories under Windows path identity."""
    file_identities = {path.casefold() for path in file_paths}
    collisions: list[str] = []
    for path in (*file_paths, *directory_paths):
        parts = path.casefold().split("/")
        if any(
            "/".join(parts[:index]) in file_identities
            for index in range(1, len(parts))
        ):
            collisions.append(path)
    return tuple(collisions)


def _contains_unsafe_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character) in _UNSAFE_UNICODE_CATEGORIES
        for character in value
    )


def _valid_product_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_PRODUCT_VERSION_CHARS
        and value == value.strip()
        and not _contains_unsafe_unicode(value)
    )


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
    return normalized.startswith((b"env:", b"keyring:", b"credential-ref:"))


def _stream_contains_secret_assignment(handle: Any) -> bool:
    overlap = b""
    first_window = True
    while True:
        chunk = handle.read(_SECRET_SCAN_CHUNK_BYTES)
        if not chunk:
            return False
        raw_window = overlap + chunk
        # Only the real file start receives a synthetic line boundary. Subsequent
        # streaming windows must inherit their boundary from actual file bytes.
        window = b"\n" + raw_window if first_window else raw_window
        first_window = False
        for match in _SECRET_ASSIGNMENT_RE.finditer(window):
            if not _secret_assignment_value_is_placeholder(match.group("value")):
                return True
        overlap = raw_window[-_SECRET_SCAN_OVERLAP_BYTES:]


def _release_content_requires_secret_scan(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    # .env.example is permitted by the path policy, but can still contain
    # accidental live credentials; its .example suffix is not in the generic set.
    return (
        path.suffix.casefold() in _SECRET_CONTENT_SUFFIXES
        or path.name.casefold() == ".env.example"
    )


def _release_file_contains_secret_assignment(relative_path: str, path: Path) -> bool:
    if not _release_content_requires_secret_scan(relative_path):
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
    if not _release_content_requires_secret_scan(_zip_member_path(member)):
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
        or _contains_unsafe_unicode(manifest.product)
    ):
        findings.append("manifest:product")
    if not _valid_product_version(manifest.version):
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
    for path in _release_file_directory_collisions(
        tuple(sorted(seen_paths | {_RELEASE_MANIFEST_NAME}))
    ):
        findings.append(f"manifest:file-directory-collision:{path}")
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
        if path.relative_to(root).as_posix() != _RELEASE_MANIFEST_NAME
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
        if path.relative_to(root).as_posix() != _RELEASE_MANIFEST_NAME
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


_ZIP_READ_ERRORS = (
    OSError,
    RuntimeError,
    NotImplementedError,
    EOFError,
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
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


def _zip_member_has_invalid_type(member: zipfile.ZipInfo) -> bool:
    # The DOS directory attribute and Unix type bits must agree with the ZIP
    # path shape. Different extractors can otherwise materialize different trees.
    if not member.is_dir() and member.external_attr & 0x10:
        return True
    if member.create_system != 3:
        return False
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    member_type = stat.S_IFMT(unix_mode)
    expected_type = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
    # ZIP writers may omit the Unix type bits entirely; that is unambiguous.
    return member_type not in (0, expected_type)


def _zip_extra_field_finding(extra: bytes) -> str | None:
    """Reject alternate entry names and malformed ZIP extra-field framing."""
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            return "member-extra-format"
        field_id = int.from_bytes(extra[offset : offset + 2], "little")
        field_size = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        if field_size > len(extra) - offset:
            return "member-extra-format"
        # Info-ZIP 0x7075 supplies a second filename. ZIP extractors and
        # Python versions differ on whether it overrides the normal name.
        # The release format needs one unambiguous Windows path identity.
        if field_id == 0x7075:
            return "unicode-path-extra"
        offset += field_size
    return None


def _zip_local_identity_finding(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    header: bytes,
    name_size: int,
    extra_size: int,
    extra: bytes,
) -> str | None:
    """Verify local CRC/sizes, ZIP64 values and deferred data descriptors."""
    deferred = bool(member.flag_bits & 0x0008)
    local_crc = int.from_bytes(header[14:18], "little")
    if local_crc != member.CRC and not (deferred and local_crc == 0):
        return "member-header-mismatch"
    local_sizes = (
        int.from_bytes(header[22:26], "little"),
        int.from_bytes(header[18:22], "little"),
    )
    expected_sizes = (member.file_size, member.compress_size)
    zip64_field: bytes | None = None
    offset = 0
    while offset < len(extra):
        field_id = int.from_bytes(extra[offset : offset + 2], "little")
        field_size = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        if field_id == 0x0001:
            if zip64_field is not None:
                return "member-header-mismatch"
            zip64_field = extra[offset : offset + field_size]
        offset += field_size

    zip64_offset = 0
    for local_size, expected_size in zip(local_sizes, expected_sizes):
        if local_size == 0xFFFFFFFF:
            if zip64_field is None or len(zip64_field) - zip64_offset < 8:
                return "member-header-mismatch"
            local_size = int.from_bytes(
                zip64_field[zip64_offset : zip64_offset + 8], "little"
            )
            zip64_offset += 8
        if local_size != expected_size and not (deferred and local_size == 0):
            return "member-header-mismatch"
    # The local ZIP64 field contains only the sizes whose ordinary header
    # values are sentinels. Unused/trailing values create alternate size
    # evidence that extractors may interpret differently.
    if zip64_field is not None and (
        zip64_offset == 0 or zip64_offset != len(zip64_field)
    ):
        return "member-header-mismatch"
    if not deferred:
        return None

    # Streaming ZIPs can defer these values, but their descriptor must agree.
    handle = archive.fp
    if handle is None:
        return "member-header-mismatch"
    descriptor_offset = (
        member.header_offset + 30 + name_size + extra_size + member.compress_size
    )
    try:
        handle.seek(descriptor_offset)
        descriptor = handle.read(24)
    except (OSError, ValueError):
        return "member-header-mismatch"
    zip64 = (
        0xFFFFFFFF in local_sizes
        or any(size >= 0xFFFFFFFF for size in expected_sizes)
    )
    width = 8 if zip64 else 4

    def matches(start: int) -> bool:
        end = start + 4 + 2 * width
        return (
            len(descriptor) >= end
            and int.from_bytes(descriptor[start : start + 4], "little") == member.CRC
            and int.from_bytes(
                descriptor[start + 4 : start + 4 + width], "little"
            ) == member.compress_size
            and int.from_bytes(descriptor[start + 4 + width : end], "little")
            == member.file_size
        )

    if not (
        matches(0)
        or (descriptor[:4] == b"PK\x07\x08" and matches(4))
    ):
        return "member-header-mismatch"
    return None


def _zip_member_extra_finding(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo
) -> str | None:
    # ZipInfo.extra contains only central-directory fields. An alternate path
    # in the local header is equally unsafe even if the central entry is clean.
    central_finding = _zip_extra_field_finding(member.extra)
    if central_finding is not None:
        return central_finding
    handle = archive.fp
    if handle is None:
        return "member-extra-format"
    try:
        handle.seek(member.header_offset)
        header = handle.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            return "member-extra-format"
        if (
            int.from_bytes(header[6:8], "little") != member.flag_bits
            or int.from_bytes(header[8:10], "little") != member.compress_type
        ):
            return "member-header-mismatch"
        filename_size = int.from_bytes(header[26:28], "little")
        extra_size = int.from_bytes(header[28:30], "little")
        local_name = handle.read(filename_size)
        local_extra = handle.read(extra_size)
    except (OSError, ValueError):
        return "member-extra-format"
    if len(local_name) != filename_size or len(local_extra) != extra_size:
        return "member-extra-format"
    try:
        encoding = "utf-8" if member.flag_bits & 0x800 else "cp437"
        if local_name.decode(encoding) != member.filename:
            return "member-local-path"
    except UnicodeError:
        return "member-local-path"
    extra_finding = _zip_extra_field_finding(local_extra)
    if extra_finding is not None:
        return extra_finding
    return _zip_local_identity_finding(
        archive, member, header, filename_size, extra_size, local_extra
    )


def _zip_member_path(member: zipfile.ZipInfo) -> str:
    if member.is_dir() and member.filename.endswith("/"):
        return member.filename[:-1]
    return member.filename


def verify_release_archive(
    artifact_path: Path,
    *,
    source_sha: str,
    expected_product_version: str | None = None,
) -> tuple[str, ...]:
    """Verify the embedded manifest against the exact files in a Windows release ZIP.

    When expected_product_version is provided, bind the embedded manifest version to
    that trusted release identity in addition to the exact source and file evidence.
    """
    normalized_source_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("archive:source-sha-format",)
    if expected_product_version is not None and not _valid_product_version(
        expected_product_version
    ):
        return ("archive:expected-product-version-format",)
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
            directory_paths: list[str] = []
            for index, member in enumerate(all_members):
                extra_finding = _zip_member_extra_finding(archive, member)
                if extra_finding is not None:
                    findings.append(f"archive:{extra_finding}:{index}")
                    continue
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
                if _zip_member_has_invalid_type(member):
                    findings.append(f"archive:member-type:{index}")
                    continue
                if member.is_dir() and member.file_size:
                    findings.append(f"archive:directory-content:{index}")
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
                if member.is_dir():
                    directory_paths.append(member_path)
                else:
                    by_path[member_path] = member
            if findings:
                return tuple(findings)
            for path in _release_file_directory_collisions(
                tuple(by_path), tuple(directory_paths)
            ):
                findings.append(f"archive:file-directory-collision:{path}")
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
            except _ZIP_READ_ERRORS:
                return ("archive:invalid-manifest",)
            manifest = _decode_release_manifest(manifest_content)
            if manifest is None:
                return ("archive:invalid-manifest",)
            structure_findings = _manifest_structure_findings(manifest)
            if structure_findings:
                return tuple(f"archive:{finding}" for finding in structure_findings)
            if manifest.source_sha != normalized_source_sha:
                findings.append("archive:source-sha")
            if (
                expected_product_version is not None
                and manifest.version != expected_product_version
            ):
                findings.append("archive:product-version")

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
                except _ZIP_READ_ERRORS:
                    findings.append(f"archive:unreadable:{relative_path}")
                    continue
                if actual_sha256 != entry.sha256:
                    findings.append(f"archive:sha256:{relative_path}")
                    continue
                try:
                    has_secret_content = _archive_member_contains_secret_assignment(archive, member)
                except _ZIP_READ_ERRORS:
                    findings.append(f"archive:unreadable:{relative_path}")
                    continue
                if has_secret_content:
                    findings.append(f"archive:secret-content:{relative_path}")
            return tuple(findings)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return ("archive:invalid-zip",)



def build_release_archive(
    bundle_dir: Path,
    artifact_path: Path,
    *,
    source_sha: str,
    expected_product_version: str,
) -> Path:
    """Publish an exact, verified ZIP including Windows-hidden bundle files.

    Assemble into a sibling temporary file and publish only after verifying
    every manifest-bound entry. Keep a previous artifact intact on failure.
    """
    if not isinstance(source_sha, str) or not _SOURCE_SHA_RE.fullmatch(source_sha):
        raise ValueError("release archive requires an exact source SHA")
    if not _valid_product_version(expected_product_version):
        raise ValueError("release archive requires an exact product version")

    root = bundle_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("release bundle must be a directory")
    if artifact_path.resolve(strict=False).is_relative_to(root):
        raise ValueError("release ZIP output must be outside its input bundle")
    files = _safe_files(root)
    manifest_path = root / _RELEASE_MANIFEST_NAME
    if manifest_path not in files:
        raise ValueError("release bundle is missing its regular manifest")
    with manifest_path.open("rb") as handle:
        raw_manifest = handle.read(_MAX_RELEASE_MANIFEST_BYTES + 1)
    if len(raw_manifest) > _MAX_RELEASE_MANIFEST_BYTES:
        raise ValueError("release bundle manifest exceeds the maximum size")
    manifest = _decode_release_manifest(raw_manifest)
    if manifest is None:
        raise ValueError("release bundle has an invalid manifest")
    if manifest.source_sha != source_sha or manifest.version != expected_product_version:
        raise ValueError("release bundle manifest does not match the requested identity")
    findings = verify_release_manifest(root, manifest)
    if findings:
        raise ValueError(f"release bundle verification failed: {findings}")

    destination = artifact_path.parent.resolve(strict=True) / artifact_path.name
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".nika-release-",
            suffix=".zip",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            # Direct enumeration includes hidden assets that Compress-Archive
            # omits, and supports ZIP64 rather than its 2-GB file limit.
            for path in files:
                archive.write(path, path.relative_to(root).as_posix())
        archive_findings = verify_release_archive(
            temporary_path,
            source_sha=source_sha,
            expected_product_version=expected_product_version,
        )
        if archive_findings:
            raise ValueError(f"release ZIP verification failed: {archive_findings}")
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


def verify_distributable_evidence(
    artifact_path: Path,
    evidence_path: Path,
    *,
    source_sha: str,
    artifact_reference: str,
    expected_product_version: str,
) -> tuple[str, ...]:
    """Verify that pre-human evidence binds the exact uploaded distributable.

    The evidence is intentionally outside the ZIP: embedding its own digest would be
    recursive. The verifier therefore binds an immutable outer artifact by path,
    byte size, SHA-256, exact source commit, and trusted canonical product version
    immediately before upload.
    """
    findings: list[str] = []
    normalized_source_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("distributable:source-sha-format",)
    if not _valid_product_version(expected_product_version):
        return ("distributable:expected-product-version-format",)
    if not artifact_path.is_file():
        return ("distributable:missing-artifact",)

    payload = _read_evidence_object(evidence_path)
    if payload is None:
        return ("distributable:invalid-evidence",)

    if frozenset(payload) != _PREHUMAN_EVIDENCE_KEYS:
        findings.append("distributable:evidence-keys")
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != _PREHUMAN_EVIDENCE_SCHEMA_VERSION
    ):
        findings.append("distributable:schema-version")

    product_version = payload.get("product_version")
    if (
        not _valid_product_version(product_version)
        or product_version != expected_product_version
    ):
        findings.append("distributable:product-version")

    for field in _PREHUMAN_REQUIRED_TRUE_FIELDS:
        if payload.get(field) is not True:
            findings.append(f"distributable:required-true:{field}")
    for field in _PREHUMAN_REQUIRED_FALSE_FIELDS:
        if payload.get(field) is not False:
            findings.append(f"distributable:required-false:{field}")

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
