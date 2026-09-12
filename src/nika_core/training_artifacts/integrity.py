from __future__ import annotations

import hashlib
import hmac
import ntpath
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from nika_core.model_artifacts import ModelArtifactDescriptor, ModelIntegrityBasis

_READ_CHUNK_BYTES = 1024 * 1024
_WINDOWS_FINAL_PATH_BUFFER = 32768


class CandidateArtifactIntegrityError(RuntimeError):
    """Safe failure from the candidate-model artifact integrity boundary."""


@dataclass(frozen=True, slots=True)
class VerifiedCandidateArtifact:
    """Minimized proof that physical bytes matched one canonical descriptor."""

    descriptor_digest: str
    registry_key: str
    sha256: str
    size_bytes: int


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _require_regular(value: os.stat_result) -> None:
    if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
        raise CandidateArtifactIntegrityError(
            "candidate artifact must not be a symbolic link or reparse point"
        )
    if not stat.S_ISREG(value.st_mode):
        raise CandidateArtifactIntegrityError("candidate artifact must be a regular file")


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact is not accessible") from exc


def _resolve_candidate_path(
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str] | None,
) -> tuple[Path, Path | None]:
    if isinstance(path, bytes):
        raise TypeError("candidate artifact path must be text or a text path-like object")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("candidate artifact path must be absolute")

    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact parent is not accessible") from exc
    resolved_candidate = resolved_parent / candidate.name

    if allowed_root is None:
        return resolved_candidate, None
    if isinstance(allowed_root, bytes):
        raise TypeError("allowed_root must be text or a text path-like object")
    root = Path(allowed_root)
    if not root.is_absolute():
        raise ValueError("allowed_root must be absolute")

    root_lstat = _safe_lstat(root)
    if stat.S_ISLNK(root_lstat.st_mode) or _is_reparse_point(root_lstat):
        raise CandidateArtifactIntegrityError(
            "allowed_root must not be a symbolic link or reparse point"
        )
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise CandidateArtifactIntegrityError("allowed_root must be a directory")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("allowed_root is not accessible") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise CandidateArtifactIntegrityError("candidate artifact is outside the allowed root")
    return resolved_candidate, resolved_root


def _open_read_only(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise CandidateArtifactIntegrityError(
            "candidate artifact could not be opened safely"
        ) from exc


def _normalize_windows_final_path(raw_path: str) -> str:
    if raw_path.startswith("\\\\?\\UNC\\"):
        raw_path = "\\\\" + raw_path[8:]
    elif raw_path.startswith("\\\\?\\"):
        raw_path = raw_path[4:]
    return ntpath.normcase(ntpath.normpath(raw_path))


def _windows_final_path(file_descriptor: int) -> str:
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(file_descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        get_final_path.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(_WINDOWS_FINAL_PATH_BUFFER)
        length = get_final_path(
            ctypes.c_void_p(handle),
            buffer,
            len(buffer),
            0,
        )
    except (ImportError, OSError, ValueError) as exc:
        raise CandidateArtifactIntegrityError(
            "candidate artifact final handle path could not be verified"
        ) from exc
    if length == 0 or length >= len(buffer):
        raise CandidateArtifactIntegrityError(
            "candidate artifact final handle path could not be verified"
        )
    return _normalize_windows_final_path(buffer.value)


def _require_windows_handle_matches_path(file_descriptor: int, path: Path) -> None:
    expected_path = _normalize_windows_final_path(ntpath.abspath(str(path)))
    if _windows_final_path(file_descriptor) != expected_path:
        raise CandidateArtifactIntegrityError(
            "candidate artifact path changed before verification"
        )


def _require_windows_handle_within_root(file_descriptor: int, root: Path) -> None:
    final_path = _windows_final_path(file_descriptor)
    normalized_root = _normalize_windows_final_path(str(root))
    try:
        common = ntpath.commonpath((normalized_root, final_path))
    except ValueError as exc:
        raise CandidateArtifactIntegrityError(
            "candidate artifact final handle escapes the allowed root"
        ) from exc
    if common != normalized_root:
        raise CandidateArtifactIntegrityError(
            "candidate artifact final handle escapes the allowed root"
        )


def _require_windows_path_still_targets_open_file(
    path: Path,
    file_descriptor: int,
    opened_identity: tuple[int, int, int, int, int, int],
    *,
    root: Path | None,
) -> None:
    _require_windows_handle_matches_path(file_descriptor, path)
    if root is not None:
        _require_windows_handle_within_root(file_descriptor, root)

    current_descriptor = _open_read_only(path)
    try:
        _require_windows_handle_matches_path(current_descriptor, path)
        if root is not None:
            _require_windows_handle_within_root(current_descriptor, root)
        try:
            current = os.fstat(current_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be re-read"
            ) from exc
        _require_regular(current)
        if _stat_identity(current) != opened_identity:
            raise CandidateArtifactIntegrityError(
                "candidate artifact path changed during verification"
            )
    finally:
        try:
            os.close(current_descriptor)
        except OSError:
            pass


def _open_posix_contained(path: Path, root: Path) -> tuple[int, os.stat_result]:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact is outside the allowed root") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise CandidateArtifactIntegrityError("candidate artifact relative path is invalid")

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if directory_flag == 0 or nofollow_flag == 0:
        raise CandidateArtifactIntegrityError(
            "platform cannot establish no-follow allowed-root containment"
        )

    directory_descriptors: list[int] = []
    try:
        root_descriptor = os.open(root, os.O_RDONLY | directory_flag | nofollow_flag)
        directory_descriptors.append(root_descriptor)
        current_descriptor = root_descriptor
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow_flag,
                dir_fd=current_descriptor,
            )
            directory_descriptors.append(next_descriptor)
            current_descriptor = next_descriptor

        before = os.stat(parts[-1], dir_fd=current_descriptor, follow_symlinks=False)
        _require_regular(before)
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | nofollow_flag,
            dir_fd=current_descriptor,
        )
        return file_descriptor, before
    except CandidateArtifactIntegrityError:
        raise
    except (OSError, TypeError, NotImplementedError) as exc:
        raise CandidateArtifactIntegrityError(
            "candidate artifact could not be opened safely within the allowed root"
        ) from exc
    finally:
        for descriptor in reversed(directory_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_contained_read_only(path: Path, root: Path) -> tuple[int, os.stat_result]:
    if os.name != "nt":
        return _open_posix_contained(path, root)

    before = _safe_lstat(path)
    _require_regular(before)
    file_descriptor = _open_read_only(path)
    try:
        _require_windows_handle_matches_path(file_descriptor, path)
        _require_windows_handle_within_root(file_descriptor, root)
    except CandidateArtifactIntegrityError:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        raise
    return file_descriptor, before


def _require_physical_descriptor(descriptor: ModelArtifactDescriptor) -> tuple[str, int]:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be a ModelArtifactDescriptor")
    if descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise CandidateArtifactIntegrityError(
            "candidate artifact requires canonical SHA-256 integrity provenance"
        )
    if descriptor.sha256 is None or descriptor.size_bytes is None:
        raise CandidateArtifactIntegrityError(
            "candidate artifact descriptor requires exact digest and size"
        )
    return descriptor.sha256, descriptor.size_bytes


def verify_candidate_artifact(
    path: str | os.PathLike[str],
    descriptor: ModelArtifactDescriptor,
    *,
    allowed_root: str | os.PathLike[str] | None = None,
) -> VerifiedCandidateArtifact:
    """Verify physical candidate bytes against canonical model provenance.

    The canonical descriptor is owned by ``nika_core.model_artifacts``. This adapter
    owns only physical byte verification. With ``allowed_root`` it binds containment
    to the object actually opened: descriptor-relative no-follow traversal on POSIX,
    and final-handle containment validation on Windows. Paths and model bytes are
    deliberately absent from returned evidence.
    """
    expected_sha256, expected_size = _require_physical_descriptor(descriptor)

    candidate, resolved_root = _resolve_candidate_path(path, allowed_root=allowed_root)
    if resolved_root is None:
        before_path = _safe_lstat(candidate)
        _require_regular(before_path)
        file_descriptor = _open_read_only(candidate)
    else:
        file_descriptor, before_path = _open_contained_read_only(candidate, resolved_root)
    before_identity = _stat_identity(before_path)

    try:
        try:
            opened = os.fstat(file_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be read"
            ) from exc
        _require_regular(opened)
        opened_identity = _stat_identity(opened)
        if os.name == "nt":
            _require_windows_handle_matches_path(file_descriptor, candidate)
            if resolved_root is not None:
                _require_windows_handle_within_root(file_descriptor, resolved_root)
        elif opened_identity != before_identity:
            raise CandidateArtifactIntegrityError(
                "candidate artifact changed before verification"
            )
        if opened.st_size != expected_size:
            raise CandidateArtifactIntegrityError(
                "candidate artifact size does not match provenance"
            )

        digest = hashlib.sha256()
        total_bytes = 0
        while True:
            remaining_with_sentinel = expected_size + 1 - total_bytes
            if remaining_with_sentinel <= 0:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact grew during verification"
                )
            try:
                chunk = os.read(
                    file_descriptor,
                    min(_READ_CHUNK_BYTES, remaining_with_sentinel),
                )
            except OSError as exc:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact could not be read"
                ) from exc
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > expected_size:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact grew during verification"
                )
            digest.update(chunk)

        if total_bytes != expected_size:
            raise CandidateArtifactIntegrityError(
                "candidate artifact size changed during verification"
            )
        try:
            after_open = os.fstat(file_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be re-read"
            ) from exc
        if _stat_identity(after_open) != opened_identity:
            raise CandidateArtifactIntegrityError(
                "candidate artifact changed during verification"
            )
        if os.name == "nt":
            _require_windows_path_still_targets_open_file(
                candidate,
                file_descriptor,
                opened_identity,
                root=resolved_root,
            )
        actual_sha256 = digest.hexdigest()
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass

    after_path = _safe_lstat(candidate)
    _require_regular(after_path)
    if after_path.st_size != expected_size:
        raise CandidateArtifactIntegrityError(
            "candidate artifact path changed during verification"
        )
    if os.name != "nt" and _stat_identity(after_path) != before_identity:
        raise CandidateArtifactIntegrityError(
            "candidate artifact path changed during verification"
        )
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise CandidateArtifactIntegrityError(
            "candidate artifact digest does not match provenance"
        )

    return VerifiedCandidateArtifact(
        descriptor_digest=descriptor.descriptor_digest,
        registry_key=descriptor.registry_key,
        sha256=expected_sha256,
        size_bytes=expected_size,
    )
