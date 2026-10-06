from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_VERSION = 2
_RELEASE_MANIFEST_NAME = "release-manifest.json"
_MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_PREHUMAN_EVIDENCE_BYTES = 1024 * 1024
_MAX_RELEASE_JSON_DEPTH = 64
_MAX_RELEASE_JSON_INTEGER_BITS = 4096
_MAX_RELEASE_JSON_INTEGER_DECIMAL_CHARS = 1234
_MAX_PRODUCT_NAME_CHARS = 128
_MAX_PRODUCT_VERSION_CHARS = 128
_MANIFEST_KEYS = frozenset({"manifest_version", "product", "version", "source_sha", "files"})
_RELEASE_FILE_KEYS = frozenset({"path", "size", "sha256"})
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|?*')
_SECRET_RELEASE_BASENAMES = frozenset(
    {
        ".env",
        "token.json",
        "tokens.json",
        "credentials.json",
        "client_secret.json",
        "client_secrets.json",
        "oauth.json",
        "oauth_credentials.json",
        "cookies.txt",
        "cookies.sqlite",
        "cookies.db",
    }
)
_SECRET_RELEASE_SUFFIXES = frozenset({".jks", ".keystore", ".p12", ".pfx", ".pkcs12", ".session"})
_SECRET_CONTENT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".key",
        ".log",
        ".pem",
        ".properties",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024
_SECRET_SCAN_OVERLAP_BYTES = 8 * 1024
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_PREHUMAN_EVIDENCE_SCHEMA_VERSION = 4
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
    "machine_readable_sbom_verified",
    "supply_chain_provenance_verified",
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
_SECRET_ASSIGNMENT_KEY_PATTERN = rb"""
    api[_-]?key|apikey|api[_-]?hash|access[_-]?token|auth[_-]?token|
    refresh[_-]?token|id[_-]?token|session[_-]?token|token|authorization|
    bearer[_-]?token|oauth[_-]?token|oauth[_-]?secret|client[_-]?secret|
    secret[_-]?key|password|passwd|private[_-]?key
"""
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\r\n{,\[]
    (?:\xef\xbb\xbf)?
    [ \t-]*
    (?P<quote>["'])?
    (?:
    """
    + _SECRET_ASSIGNMENT_KEY_PATTERN
    + rb"""
    )
    (?(quote)(?P=quote))
    \s*[:=]\s*
    (?P<value>
        "(?:\\.|[^"\\\r\n]){1,4096}"|
        '(?:\\.|[^'\\\r\n]){1,4096}'|
        # Complete unquoted environment references are non-secret placeholders.
        \$\{[A-Za-z_][A-Za-z0-9_]*\}(?=[\s,;\#}\]\r\n]|$)|
        [^\s,\#;}{\]\r\n]{1,4096}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NAMESPACED_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\r\n]
    (?:\xef\xbb\xbf)?
    [ \t-]*
    (?:[A-Za-z][A-Za-z0-9]*[_-])+
    (?:
    """
    + _SECRET_ASSIGNMENT_KEY_PATTERN
    + rb"""
    )
    \s*=\s*
    (?P<value>
        "(?:\\.|[^"\\\r\n]){1,4096}"|
        '(?:\\.|[^'\\\r\n]){1,4096}'|
        \$\{[A-Za-z_][A-Za-z0-9_]*\}(?=[\s,;\#}\]\r\n]|$)|
        [^\s,\#;}{\]\r\n]{1,4096}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_OVERSIZED_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\r\n{,\[]
    (?:\xef\xbb\xbf)?
    [ \t-]*
    (?P<quote>["'])?
    (?:
    """
    + _SECRET_ASSIGNMENT_KEY_PATTERN
    + rb"""
    )
    (?(quote)(?P=quote))
    \s*[:=]\s*
    (?:
        "(?:\\.|[^"\\\r\n]){4097}|
        '(?:\\.|[^'\\\r\n]){4097}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_OVERSIZED_NAMESPACED_SECRET_ASSIGNMENT_RE = re.compile(
    rb"""
    [\r\n]
    (?:\xef\xbb\xbf)?
    [ \t-]*
    (?:[A-Za-z][A-Za-z0-9]*[_-])+
    (?:
    """
    + _SECRET_ASSIGNMENT_KEY_PATTERN
    + rb"""
    )
    \s*=\s*
    (?:
        "(?:\\.|[^"\\\r\n]){4097}|
        '(?:\\.|[^'\\\r\n]){4097}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PRIVATE_KEY_PEM_RE = re.compile(
    rb"-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE,
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


@dataclass(frozen=True, slots=True)
class _ReleaseFileSnapshot:
    size: int
    sha256: str
    contains_secret_assignment: bool


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
        # Explicit iteration must fail rather than certify an incomplete release.
        for candidate in directories.pop().iterdir():
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
                raise ValueError(f"unsupported release bundle entry: {candidate}")
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def _canonical_relative_path(value: object) -> bool:
    if type(value) is not str or not value or "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if unicodedata.normalize("NFC", value) != value:
        return False
    if any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value):
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
    if type(value) is not str:
        return False
    for part in PurePosixPath(value).parts:
        identity = part.casefold()
        if identity in _SECRET_RELEASE_BASENAMES:
            return True
        if any(identity.endswith(suffix) for suffix in _SECRET_RELEASE_SUFFIXES):
            return True
        if identity.startswith(".env.") and identity != ".env.example":
            return True
    return False


def _canonical_release_path(value: object) -> bool:
    return (
        _canonical_relative_path(value)
        and type(value) is str
        and value.casefold() != _RELEASE_MANIFEST_NAME
    )


def _valid_product_name(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= _MAX_PRODUCT_NAME_CHARS
        and value == value.strip()
        and not any(ord(character) < 32 for character in value)
    )


def _valid_product_version(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= _MAX_PRODUCT_VERSION_CHARS
        and value == value.strip()
        and not any(ord(character) < 32 for character in value)
    )


def _secret_assignment_value_is_placeholder(value: bytes) -> bool:
    normalized = value.strip().strip(b"\"'").strip().lower()
    for prefix in (b"bearer ", b"basic "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    if not normalized or normalized in _SECRET_PLACEHOLDER_VALUES:
        return True
    if normalized.startswith(b"${") and normalized.endswith(b"}"):
        return True
    if normalized.startswith(b"{{") and normalized.endswith(b"}}"):
        return True
    if normalized.startswith(b"%") and normalized.endswith(b"%") and len(normalized) > 2:
        return True
    return normalized.startswith((b"env:", b"keyring:", b"credential-ref:"))


def _window_contains_secret_assignment(
    window: bytes,
    *,
    scan_namespaced_secrets: bool,
) -> bool:
    if _PRIVATE_KEY_PEM_RE.search(window):
        return True
    if _OVERSIZED_QUOTED_SECRET_ASSIGNMENT_RE.search(window):
        return True
    if (
        scan_namespaced_secrets
        and _OVERSIZED_NAMESPACED_SECRET_ASSIGNMENT_RE.search(window)
    ):
        return True
    patterns = (_SECRET_ASSIGNMENT_RE,)
    if scan_namespaced_secrets:
        patterns += (_NAMESPACED_SECRET_ASSIGNMENT_RE,)
    for pattern in patterns:
        for match in pattern.finditer(window):
            if not _secret_assignment_value_is_placeholder(match.group("value")):
                return True
    return False


def _stream_contains_secret_assignment(
    handle: Any,
    *,
    scan_namespaced_secrets: bool = False,
) -> bool:
    overlap = b""
    first_window = True
    while True:
        chunk = handle.read(_SECRET_SCAN_CHUNK_BYTES)
        if not chunk:
            return False
        raw_window = overlap + chunk
        window = b"\n" + raw_window if first_window else raw_window
        first_window = False
        if _window_contains_secret_assignment(
            window,
            scan_namespaced_secrets=scan_namespaced_secrets,
        ):
            return True
        overlap = raw_window[-_SECRET_SCAN_OVERLAP_BYTES:]


def _release_content_requires_secret_scan(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return (
        path.suffix.casefold() in _SECRET_CONTENT_SUFFIXES
        or path.name.casefold() == ".env.example"
    )


def _archive_member_contains_secret_assignment(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
) -> bool:
    relative_path = _zip_member_path(member)
    if not _release_content_requires_secret_scan(relative_path):
        return False
    with archive.open(member, "r") as handle:
        return _stream_contains_secret_assignment(
            handle,
            scan_namespaced_secrets=(
                PurePosixPath(relative_path).name.casefold() == ".env.example"
            ),
        )


def _manifest_structure_findings(manifest: ReleaseManifest) -> tuple[str, ...]:
    if type(manifest) is not ReleaseManifest:
        return ("manifest:type",)
    findings: list[str] = []
    if type(manifest.manifest_version) is not int or manifest.manifest_version != _MANIFEST_VERSION:
        findings.append("manifest:schema-version")
    if not _valid_product_name(manifest.product):
        findings.append("manifest:product")
    if not _valid_product_version(manifest.version):
        findings.append("manifest:product-version")
    if (
        type(manifest.source_sha) is not str
        or not _SOURCE_SHA_RE.fullmatch(manifest.source_sha)
    ):
        findings.append("manifest:source-sha")
    if type(manifest.files) is not tuple or not manifest.files:
        findings.append("manifest:files")
        return tuple(findings)

    seen_paths: set[str] = set()
    seen_windows_paths: set[str] = set()
    for index, entry in enumerate(manifest.files):
        if type(entry) is not ReleaseFile:
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
        if type(entry.sha256) is not str or not _SHA256_RE.fullmatch(entry.sha256):
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


def _release_file_directory_collisions(
    file_paths: tuple[str, ...],
    directory_paths: tuple[str, ...] = (),
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


def _stream_release_file_snapshot(
    handle: Any,
    *,
    scan_secrets: bool,
    scan_namespaced_secrets: bool = False,
) -> _ReleaseFileSnapshot:
    digest = hashlib.sha256()
    size = 0
    overlap = b""
    first_window = True
    contains_secret_assignment = False

    while True:
        chunk = handle.read(_SECRET_SCAN_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
        if not scan_secrets or contains_secret_assignment:
            continue

        raw_window = overlap + chunk
        window = b"\n" + raw_window if first_window else raw_window
        first_window = False
        if _window_contains_secret_assignment(
            window,
            scan_namespaced_secrets=scan_namespaced_secrets,
        ):
            contains_secret_assignment = True
        overlap = raw_window[-_SECRET_SCAN_OVERLAP_BYTES:]

    return _ReleaseFileSnapshot(
        size=size,
        sha256=digest.hexdigest(),
        contains_secret_assignment=contains_secret_assignment,
    )


def _release_file_snapshot_is_stable(
    before: os.stat_result,
    after: os.stat_result,
    current: os.stat_result,
    observed_size: int,
) -> bool:
    if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(after.st_mode):
        return False
    if observed_size != before.st_size or observed_size != after.st_size:
        return False
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        return False
    if not os.path.samestat(after, current):
        return False
    return (
        current.st_size == after.st_size
        and current.st_mtime_ns == after.st_mtime_ns
        and current.st_ctime_ns == after.st_ctime_ns
    )


def _open_release_file_for_snapshot(path: Path) -> Any:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
        except ImportError as exc:
            raise OSError("Windows release snapshot support is unavailable") from exc

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(path),
            _WINDOWS_GENERIC_READ,
            _WINDOWS_FILE_SHARE_READ,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")

        try:
            descriptor = msvcrt.open_osfhandle(
                int(handle),
                os.O_RDONLY
                | int(getattr(os, "O_BINARY", 0))
                | int(getattr(os, "O_NOINHERIT", 0)),
            )
        except (OSError, OverflowError, ValueError):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise
    else:
        flags = os.O_RDONLY
        for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK"):
            flags |= getattr(os, flag_name, 0)
        descriptor = os.open(path, flags)

    try:
        return os.fdopen(descriptor, "rb", closefd=True)
    except (OSError, ValueError):
        os.close(descriptor)
        raise


def _stable_release_file_snapshot(
    path: Path,
    *,
    scan_secrets: bool,
    root: Path | None = None,
) -> _ReleaseFileSnapshot | None:
    try:
        with _open_release_file_for_snapshot(path) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            snapshot = _stream_release_file_snapshot(
                handle,
                scan_secrets=scan_secrets,
                scan_namespaced_secrets=(
                    path.name.casefold() == ".env.example"
                ),
            )
            after = os.fstat(handle.fileno())
        if root is None:
            current = path.stat()
        else:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            current = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not _release_file_snapshot_is_stable(before, after, current, snapshot.size):
        return None
    return snapshot


def build_release_manifest(
    bundle_dir: Path,
    *,
    product: str,
    version: str,
    source_sha: str,
) -> ReleaseManifest:
    root = bundle_dir.resolve(strict=True)
    entries_list: list[ReleaseFile] = []
    for path in _safe_files(root):
        relative_path = path.relative_to(root).as_posix()
        if relative_path == _RELEASE_MANIFEST_NAME:
            continue
        snapshot = _stable_release_file_snapshot(
            path,
            scan_secrets=False,
            root=root,
        )
        if snapshot is None:
            raise ValueError(f"release file changed while building manifest: {relative_path}")
        entries_list.append(
            ReleaseFile(
                path=relative_path,
                size=snapshot.size,
                sha256=snapshot.sha256,
            )
        )
    entries = tuple(entries_list)
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
        scan_secrets = _release_content_requires_secret_scan(relative_path)
        snapshot = _stable_release_file_snapshot(
            path,
            scan_secrets=scan_secrets,
            root=root,
        )
        if snapshot is None:
            findings.append(f"unstable:{relative_path}")
            continue
        if snapshot.size != entry.size:
            findings.append(f"size:{relative_path}")
            continue
        if snapshot.sha256 != entry.sha256:
            findings.append(f"sha256:{relative_path}")
            continue
        if snapshot.contains_secret_assignment:
            findings.append(f"secret-content:{relative_path}")
    return tuple(findings)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _finite_json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _bounded_json_int(raw: str) -> int:
    digits = raw.removeprefix("-")
    if len(digits) > _MAX_RELEASE_JSON_INTEGER_DECIMAL_CHARS:
        raise ValueError("release JSON integer exceeds the digit limit")
    value = int(raw)
    if value.bit_length() > _MAX_RELEASE_JSON_INTEGER_BITS:
        raise ValueError("release JSON integer exceeds the bit limit")
    return value


def _reject_json_constant(_raw: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _bounded_json_depth(content: bytes) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for byte in content:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
        elif byte == 0x22:
            quoted = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_RELEASE_JSON_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _decode_json_object(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_unique_json_object,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_int,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _read_evidence_object(evidence_path: Path) -> dict[str, Any] | None:
    try:
        with evidence_path.open("rb") as handle:
            content = handle.read(_MAX_PREHUMAN_EVIDENCE_BYTES + 1)
    except OSError:
        return None
    if len(content) > _MAX_PREHUMAN_EVIDENCE_BYTES or not _bounded_json_depth(content):
        return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return None
    return _decode_json_object(text)


def _decode_release_manifest(content: bytes) -> ReleaseManifest | None:
    if len(content) > _MAX_RELEASE_MANIFEST_BYTES or not _bounded_json_depth(content):
        return None
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


def verify_release_archive(
    artifact_path: Path,
    *,
    source_sha: str,
    expected_product: str | None = None,
    expected_product_version: str | None = None,
) -> tuple[str, ...]:
    """Verify the embedded manifest against the exact files in a Windows release ZIP.

    When expected_product_version is provided, bind the embedded manifest version to
    that trusted release identity in addition to the exact source and file evidence.
    """
    if type(source_sha) is not str:
        return ("archive:source-sha-format",)
    normalized_source_sha = source_sha.casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("archive:source-sha-format",)
    if expected_product is not None and not _valid_product_name(expected_product):
        return ("archive:expected-product-format",)
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
                if member.is_dir():
                    directory_paths.append(member_path)
                else:
                    by_path[member_path] = member
            if findings:
                return tuple(findings)
            for path in _release_file_directory_collisions(
                tuple(by_path),
                tuple(directory_paths),
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
            except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
                return ("archive:invalid-manifest",)
            manifest = _decode_release_manifest(manifest_content)
            if manifest is None:
                return ("archive:invalid-manifest",)
            structure_findings = _manifest_structure_findings(manifest)
            if structure_findings:
                return tuple(f"archive:{finding}" for finding in structure_findings)
            if expected_product is not None and manifest.product != expected_product:
                findings.append("archive:product")
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
    expected_product_version: str,
) -> tuple[str, ...]:
    """Verify that pre-human evidence binds the exact uploaded distributable.

    The evidence is intentionally outside the ZIP: embedding its own digest would be
    recursive. The verifier therefore binds an immutable outer artifact by path,
    byte size, SHA-256, exact source commit, and trusted canonical product version
    immediately before upload.
    """
    findings: list[str] = []
    if type(source_sha) is not str:
        return ("distributable:source-sha-format",)
    normalized_source_sha = source_sha.casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_source_sha):
        return ("distributable:source-sha-format",)
    if type(artifact_reference) is not str:
        return ("distributable:artifact-reference-format",)
    if not _valid_product_version(expected_product_version):
        return ("distributable:expected-product-version-format",)
    if not artifact_path.is_file():
        return ("distributable:missing-artifact",)
    artifact_snapshot = _stable_release_file_snapshot(artifact_path, scan_secrets=False)
    if artifact_snapshot is None:
        return ("distributable:unstable-artifact",)

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
    elif artifact_snapshot.size != expected_size:
        findings.append("distributable:size")

    expected_sha256 = payload.get("distributable_zip_sha256")
    if type(expected_sha256) is not str or not _SHA256_RE.fullmatch(expected_sha256):
        findings.append("distributable:sha256-format")
    elif artifact_snapshot.sha256 != expected_sha256:
        findings.append("distributable:sha256")
    return tuple(findings)
