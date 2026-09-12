from __future__ import annotations

import hashlib
import hmac
import json
import ntpath
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore

_SCHEMA_VERSION = 1
_MAX_WORKSPACE_BYTES = 4096
_MAX_SIGNED_64 = (1 << 63) - 1
_HEX_DIGITS = frozenset("0123456789abcdef")
_READ_CHUNK_BYTES = 1024 * 1024
_PLATFORM_PATH_TYPE = type(Path())
_WINDOWS_FINAL_PATH_BUFFER = 32768


class TrainingMaterialResolutionError(RuntimeError):
    """Frozen learning material could not be resolved to exact physical bytes."""


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(name: str, value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _require_positive_int(name: str, value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_64:
        raise ValueError(f"{name} must be a positive signed-64 integer")
    return value


def _workspace_fingerprint(workspace_id: str) -> str:
    if type(workspace_id) is not str:
        raise TypeError("workspace_id must be text")
    if not workspace_id or workspace_id != workspace_id.strip():
        raise ValueError("workspace_id must be non-empty without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in workspace_id):
        raise ValueError("workspace_id must not contain control characters")
    encoded = workspace_id.encode("utf-8")
    if len(encoded) > _MAX_WORKSPACE_BYTES:
        raise ValueError("workspace_id exceeds the configured byte limit")
    return hashlib.sha256(encoded).hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise TrainingMaterialResolutionError("resolved training material is not accessible") from exc


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _require_regular_material(value: os.stat_result) -> None:
    if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
        raise TrainingMaterialResolutionError(
            "resolved training material must not be a symbolic link or reparse point"
        )
    if not stat.S_ISREG(value.st_mode):
        raise TrainingMaterialResolutionError("resolved training material must be a regular file")


def _open_read_only(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise TrainingMaterialResolutionError(
            "resolved training material could not be opened safely"
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
        raise TrainingMaterialResolutionError(
            "resolved training material handle path could not be verified"
        ) from exc
    if length == 0 or length >= len(buffer):
        raise TrainingMaterialResolutionError(
            "resolved training material handle path could not be verified"
        )
    return _normalize_windows_final_path(buffer.value)


def _require_windows_handle_matches_path(file_descriptor: int, path: Path) -> None:
    expected_path = _normalize_windows_final_path(ntpath.abspath(str(path)))
    if _windows_final_path(file_descriptor) != expected_path:
        raise TrainingMaterialResolutionError(
            "resolved training material path changed before verification"
        )


def _require_windows_path_still_targets_open_file(
    path: Path,
    file_descriptor: int,
    opened_identity: tuple[int, int, int, int, int, int],
) -> None:
    _require_windows_handle_matches_path(file_descriptor, path)
    current_descriptor = _open_read_only(path)
    try:
        _require_windows_handle_matches_path(current_descriptor, path)
        try:
            current = os.fstat(current_descriptor)
        except OSError as exc:
            raise TrainingMaterialResolutionError(
                "resolved training material metadata could not be re-read"
            ) from exc
        _require_regular_material(current)
        if _stat_identity(current) != opened_identity:
            raise TrainingMaterialResolutionError(
                "resolved training material path changed during verification"
            )
    finally:
        try:
            os.close(current_descriptor)
        except OSError:
            pass


def _verify_resolved_material(material: ResolvedTrainingMaterial) -> None:
    """Bind one transient path to the exact evidence bytes at the point of use."""
    path = material.path
    evidence = material.evidence
    before = _safe_lstat(path)
    _require_regular_material(before)
    before_identity = _stat_identity(before)
    if before.st_size != evidence.byte_count:
        raise TrainingMaterialResolutionError(
            "resolved training material size does not match frozen evidence"
        )

    file_descriptor = _open_read_only(path)
    try:
        try:
            opened = os.fstat(file_descriptor)
        except OSError as exc:
            raise TrainingMaterialResolutionError(
                "resolved training material metadata could not be read"
            ) from exc
        _require_regular_material(opened)
        opened_identity = _stat_identity(opened)
        if os.name == "nt":
            _require_windows_handle_matches_path(file_descriptor, path)
        elif opened_identity != before_identity:
            raise TrainingMaterialResolutionError(
                "resolved training material changed before verification"
            )
        if opened.st_size != evidence.byte_count:
            raise TrainingMaterialResolutionError(
                "resolved training material size does not match frozen evidence"
            )

        digest = hashlib.sha256()
        total_bytes = 0
        while True:
            remaining_with_sentinel = evidence.byte_count + 1 - total_bytes
            if remaining_with_sentinel <= 0:
                raise TrainingMaterialResolutionError(
                    "resolved training material grew during verification"
                )
            try:
                chunk = os.read(
                    file_descriptor,
                    min(_READ_CHUNK_BYTES, remaining_with_sentinel),
                )
            except OSError as exc:
                raise TrainingMaterialResolutionError(
                    "resolved training material could not be read"
                ) from exc
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > evidence.byte_count:
                raise TrainingMaterialResolutionError(
                    "resolved training material grew during verification"
                )
            digest.update(chunk)

        if total_bytes != evidence.byte_count:
            raise TrainingMaterialResolutionError(
                "resolved training material size changed during verification"
            )
        try:
            after_open = os.fstat(file_descriptor)
        except OSError as exc:
            raise TrainingMaterialResolutionError(
                "resolved training material metadata could not be re-read"
            ) from exc
        if _stat_identity(after_open) != opened_identity:
            raise TrainingMaterialResolutionError(
                "resolved training material changed during verification"
            )
        if os.name == "nt":
            _require_windows_path_still_targets_open_file(
                path,
                file_descriptor,
                opened_identity,
            )
        actual_sha256 = digest.hexdigest()
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass

    after_path = _safe_lstat(path)
    _require_regular_material(after_path)
    if after_path.st_size != evidence.byte_count:
        raise TrainingMaterialResolutionError(
            "resolved training material path changed during verification"
        )
    if os.name != "nt" and _stat_identity(after_path) != before_identity:
        raise TrainingMaterialResolutionError(
            "resolved training material path changed during verification"
        )
    if not hmac.compare_digest(actual_sha256, evidence.artifact_sha256):
        raise TrainingMaterialResolutionError(
            "resolved training material digest does not match frozen evidence"
        )


@dataclass(frozen=True, slots=True)
class TrainingMaterialEvidence:
    """Secret-minimized binding for one resolved frozen learning shard."""

    split: LearningDataSplit
    artifact_sha256: str
    provenance_sha256: str
    license_evidence_sha256: str
    record_count: int
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.split) is not LearningDataSplit:
            raise TypeError("split must be an exact LearningDataSplit")
        _require_sha256("artifact_sha256", self.artifact_sha256)
        _require_sha256("provenance_sha256", self.provenance_sha256)
        _require_sha256("license_evidence_sha256", self.license_evidence_sha256)
        _require_positive_int("record_count", self.record_count)
        _require_positive_int("byte_count", self.byte_count)

    @classmethod
    def from_shard(cls, shard: LearningShard) -> TrainingMaterialEvidence:
        if type(shard) is not LearningShard:
            raise TypeError("shard must be an exact LearningShard")
        return cls(
            split=shard.split,
            artifact_sha256=shard.artifact_sha256,
            provenance_sha256=shard.provenance_sha256,
            license_evidence_sha256=shard.license_evidence_sha256,
            record_count=shard.record_count,
            byte_count=shard.byte_count,
        )

    def to_learning_shard(self) -> LearningShard:
        return LearningShard(
            split=self.split,
            artifact_sha256=self.artifact_sha256,
            provenance_sha256=self.provenance_sha256,
            license_evidence_sha256=self.license_evidence_sha256,
            record_count=self.record_count,
            byte_count=self.byte_count,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "byte_count": self.byte_count,
            "license_evidence_sha256": self.license_evidence_sha256,
            "provenance_sha256": self.provenance_sha256,
            "record_count": self.record_count,
            "split": self.split.value,
        }


@dataclass(frozen=True, slots=True)
class TrainingMaterialSetEvidence:
    """Self-verifying durable evidence for exact bytes resolved toward one training job."""

    workspace_sha256: str
    package_id: str
    package_version: str
    package_schema_version: int
    base_artifact_sha256: str
    selection_policy_sha256: str
    verification_sha256: str
    evaluation_set_sha256: str
    candidate_dataset_sha256: str
    package_manifest_sha256: str
    materials: tuple[TrainingMaterialEvidence, ...]
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported training material evidence schema")
        _require_sha256("workspace_sha256", self.workspace_sha256)
        _require_sha256("base_artifact_sha256", self.base_artifact_sha256)
        _require_sha256("selection_policy_sha256", self.selection_policy_sha256)
        _require_sha256("verification_sha256", self.verification_sha256)
        _require_sha256("evaluation_set_sha256", self.evaluation_set_sha256)
        _require_sha256("candidate_dataset_sha256", self.candidate_dataset_sha256)
        _require_sha256("package_manifest_sha256", self.package_manifest_sha256)
        if type(self.materials) is not tuple or not self.materials:
            raise ValueError("materials must be a non-empty immutable tuple")
        if not all(type(material) is TrainingMaterialEvidence for material in self.materials):
            raise TypeError("materials must contain exact TrainingMaterialEvidence values")

        reconstructed = FrozenLearningPackage(
            package_id=self.package_id,
            package_version=self.package_version,
            base_artifact_sha256=self.base_artifact_sha256,
            selection_policy_sha256=self.selection_policy_sha256,
            verification_sha256=self.verification_sha256,
            evaluation_set_sha256=self.evaluation_set_sha256,
            shards=tuple(material.to_learning_shard() for material in self.materials),
            schema_version=self.package_schema_version,
        )
        if reconstructed.candidate_dataset_sha256 != self.candidate_dataset_sha256:
            raise ValueError("candidate dataset digest does not match resolved materials")
        if reconstructed.manifest_sha256 != self.package_manifest_sha256:
            raise ValueError("package manifest digest does not match resolved materials")

    @classmethod
    def from_package(
        cls,
        package: FrozenLearningPackage,
        *,
        workspace_sha256: str,
        materials: tuple[TrainingMaterialEvidence, ...],
    ) -> TrainingMaterialSetEvidence:
        if type(package) is not FrozenLearningPackage:
            raise TypeError("package must be an exact FrozenLearningPackage")
        return cls(
            workspace_sha256=workspace_sha256,
            package_id=package.package_id,
            package_version=package.package_version,
            package_schema_version=package.schema_version,
            base_artifact_sha256=package.base_artifact_sha256,
            selection_policy_sha256=package.selection_policy_sha256,
            verification_sha256=package.verification_sha256,
            evaluation_set_sha256=package.evaluation_set_sha256,
            candidate_dataset_sha256=package.candidate_dataset_sha256,
            package_manifest_sha256=package.manifest_sha256,
            materials=materials,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_artifact_sha256": self.base_artifact_sha256,
            "candidate_dataset_sha256": self.candidate_dataset_sha256,
            "evaluation_set_sha256": self.evaluation_set_sha256,
            "materials": [material.canonical_payload() for material in self.materials],
            "package_id": self.package_id,
            "package_manifest_sha256": self.package_manifest_sha256,
            "package_schema_version": self.package_schema_version,
            "package_version": self.package_version,
            "schema_version": self.schema_version,
            "selection_policy_sha256": self.selection_policy_sha256,
            "verification_sha256": self.verification_sha256,
            "workspace_sha256": self.workspace_sha256,
        }

    @property
    def training_material_sha256(self) -> str:
        return _sha256_payload(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ResolvedTrainingMaterial:
    """Transient execution-only path paired with durable-safe shard evidence."""

    evidence: TrainingMaterialEvidence
    path: Path

    def __post_init__(self) -> None:
        if type(self.evidence) is not TrainingMaterialEvidence:
            raise TypeError("evidence must be exact TrainingMaterialEvidence")
        if type(self.path) is not _PLATFORM_PATH_TYPE or not self.path.is_absolute():
            raise ValueError("resolved training path must be an absolute canonical platform Path")


@dataclass(frozen=True, slots=True)
class ResolvedTrainingPackage:
    """Transient resolved paths plus durable-safe exact material-set evidence."""

    evidence: TrainingMaterialSetEvidence
    materials: tuple[ResolvedTrainingMaterial, ...]

    def __post_init__(self) -> None:
        if type(self.evidence) is not TrainingMaterialSetEvidence:
            raise TypeError("evidence must be exact TrainingMaterialSetEvidence")
        if type(self.materials) is not tuple or not self.materials:
            raise ValueError("materials must be a non-empty immutable tuple")
        if not all(type(material) is ResolvedTrainingMaterial for material in self.materials):
            raise TypeError("materials must contain exact ResolvedTrainingMaterial values")
        if tuple(material.evidence for material in self.materials) != self.evidence.materials:
            raise ValueError("resolved paths do not match durable material evidence")
        self.reverify()

    @property
    def training_material_sha256(self) -> str:
        return self.evidence.training_material_sha256

    def reverify(self) -> None:
        """Re-bind every transient path to frozen byte evidence before a trainer effect."""
        for material in self.materials:
            _verify_resolved_material(material)


def resolve_training_materials(
    package: FrozenLearningPackage,
    *,
    workspace_id: str,
    blob_store: ContentAddressedBlobStore,
) -> ResolvedTrainingPackage:
    """Re-resolve and reverify every frozen TRAINING/VALIDATION shard.

    This function is intentionally effect-free beyond bounded filesystem reads. A caller
    must invoke it again immediately before every initial or resumed trainer effect; a
    previous resolution is evidence only and never grants replay authority.
    """
    if type(package) is not FrozenLearningPackage:
        raise TypeError("package must be an exact FrozenLearningPackage")
    if type(blob_store) is not ContentAddressedBlobStore:
        raise TypeError("blob_store must be the canonical ContentAddressedBlobStore")
    workspace_sha256 = _workspace_fingerprint(workspace_id)

    resolved: list[ResolvedTrainingMaterial] = []
    evidence_items: list[TrainingMaterialEvidence] = []
    for shard in package.shards:
        if shard.split not in (LearningDataSplit.TRAINING, LearningDataSplit.VALIDATION):
            raise TrainingMaterialResolutionError(
                "frozen package contains a non-training material split"
            )
        try:
            path = blob_store.resolve_digest(
                workspace_id,
                shard.artifact_sha256,
                shard.byte_count,
            )
        except BlobStoreError as exc:
            raise TrainingMaterialResolutionError(
                "frozen learning shard bytes are unavailable or inconsistent"
            ) from exc
        item_evidence = TrainingMaterialEvidence.from_shard(shard)
        evidence_items.append(item_evidence)
        resolved.append(ResolvedTrainingMaterial(evidence=item_evidence, path=path))

    material_set_evidence = TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=workspace_sha256,
        materials=tuple(evidence_items),
    )
    return ResolvedTrainingPackage(
        evidence=material_set_evidence,
        materials=tuple(resolved),
    )
