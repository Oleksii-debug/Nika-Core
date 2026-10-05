from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

_PROTOCOL_VERSION = 3
_SCHEMA_VERSION = 1
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 100_000
_MAX_LINE_BYTES = 256 * 1024
_MAX_RECORDS_DEFAULT = 50_000
_MAX_RECORDS_LIMIT = 1_000_000
_MAX_SEQUENCE_LENGTH_DEFAULT = 1024
_MAX_SEQUENCE_LENGTH_LIMIT = 8192
_MAX_TEXT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:+/-]{1,256}$")
_MATERIAL_DOMAIN = b"nika-training-consumed-materials-v1\x00"
_CHECKPOINT_PAYLOAD_DOMAIN = b"nika-peft-checkpoint-payload-v1\x00"
_CHECKPOINT_MARKER = "nika_checkpoint.json"
_CANDIDATE_FILE = "adapter_model.safetensors"
_MAX_CHECKPOINT_FILES = 4096
_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024 * 1024
_MAX_RUNTIME_VERSION_BYTES = 256
_TRAINING_RUNTIME_DISTRIBUTIONS = (
    ("torch", "NIKA_TRAINER_TORCH_VERSION"),
    ("transformers", "NIKA_TRAINER_TRANSFORMERS_VERSION"),
    ("peft", "NIKA_TRAINER_PEFT_VERSION"),
    ("accelerate", "NIKA_TRAINER_ACCELERATE_VERSION"),
    ("gguf", "NIKA_TRAINER_GGUF_VERSION"),
    ("safetensors", "NIKA_TRAINER_SAFETENSORS_VERSION"),
)
_MAX_MODEL_DIR_FILES = 10_000
_MAX_MODEL_DIR_BYTES = 16 * 1024 * 1024 * 1024
_MODEL_SNAPSHOT_DIR = "model-snapshot"


class PeftTrainerError(RuntimeError):
    """Fail-closed trainer-process error without secret-bearing public details."""


@dataclass(frozen=True, slots=True)
class MaterialRequest:
    split: str
    artifact_sha256: str
    byte_count: int
    path: Path


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    step_id: str
    step_index: int
    job_fingerprint: str
    candidate_artifact_ref: str
    base_artifact_ref: str
    base_artifact_sha256: str
    max_steps: int
    required_consumed_materials_sha256: str
    materials: tuple[MaterialRequest, ...]
    resume_state: dict[str, object]


@dataclass(frozen=True, slots=True)
class TrainerConfig:
    base_gguf: Path
    model_dir: Path
    model_dir_manifest_sha256: str
    output_root: Path
    max_records: int
    max_sequence_length: int
    learning_rate: float
    lora_r: int
    lora_alpha: int
    lora_dropout: float
    lora_target_modules: tuple[str, ...]
    torch_num_threads: int
    seed: int


@dataclass(frozen=True, slots=True)
class TrainingExample:
    prompt: str
    response: str


@dataclass(frozen=True, slots=True)
class ConsumedMaterials:
    training: tuple[TrainingExample, ...]
    validation: tuple[TrainingExample, ...]
    attestation_sha256: str


def _fail(code: str) -> NoReturn:
    raise PeftTrainerError(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        _fail("file_read_failed")
    return digest.hexdigest()


def trainer_implementation_sha256() -> str:
    """Digest the exact installed trainer source used by a product execution plan."""
    return _sha256_file(Path(__file__).resolve())


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str or _HEX_RE.fullmatch(value) is None:
        _fail(f"{field}_invalid")
    return value


def _require_bounded_text(value: object, *, field: str, max_bytes: int = 4096) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(f"{field}_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(f"{field}_invalid")
    if len(encoded) > max_bytes or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _fail(f"{field}_invalid")
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite constant")


def _bounded_json_tree(value: object) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("json bounds")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("non-finite")
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("non-string key")
                visit(child, depth + 1)
            return
        raise ValueError("non-json value")

    visit(value, 0)


def _read_request() -> dict[str, object]:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        _fail("request_too_large")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        _bounded_json_tree(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("request_invalid_json")
    if type(value) is not dict:
        _fail("request_not_object")
    return value


def _parse_material(value: object) -> MaterialRequest:
    if type(value) is not dict:
        _fail("material_invalid")
    if set(value) != {"artifact_sha256", "byte_count", "path", "split"}:
        _fail("material_fields_invalid")
    split = value["split"]
    if split not in {"training", "validation"}:
        _fail("material_split_invalid")
    digest = _require_sha256(value["artifact_sha256"], field="material_sha256")
    byte_count = value["byte_count"]
    if type(byte_count) is not int or not 1 <= byte_count <= (1 << 63) - 1:
        _fail("material_byte_count_invalid")
    raw_path = _require_bounded_text(value["path"], field="material_path", max_bytes=32 * 1024)
    path = Path(raw_path)
    if not path.is_absolute():
        _fail("material_path_not_absolute")
    return MaterialRequest(
        split=split,
        artifact_sha256=digest,
        byte_count=byte_count,
        path=path,
    )


def _parse_request(value: dict[str, object]) -> ParsedRequest:
    expected = {
        "command_artifacts",
        "command_sha256",
        "job",
        "job_fingerprint",
        "previous_step_id",
        "protocol_version",
        "resume_state",
        "step_id",
        "step_index",
        "trainer_artifact_id",
        "trainer_sha256",
        "training_materials",
    }
    if set(value) != expected:
        _fail("request_fields_invalid")
    if value["protocol_version"] != _PROTOCOL_VERSION:
        _fail("protocol_unsupported")
    step_id = _require_sha256(value["step_id"], field="step_id")
    job_fingerprint = _require_sha256(value["job_fingerprint"], field="job_fingerprint")
    _require_sha256(value["command_sha256"], field="command_sha256")
    _require_sha256(value["trainer_artifact_id"], field="trainer_artifact_id")
    _require_sha256(value["trainer_sha256"], field="trainer_sha256")

    step_index = value["step_index"]
    if type(step_index) is not int or step_index < 0:
        _fail("step_index_invalid")
    job = value["job"]
    if type(job) is not dict:
        _fail("job_invalid")
    required_job_keys = {
        "base_artifact",
        "candidate_artifact_ref",
        "command_sha256",
        "frozen_package_sha256",
        "job_id",
        "max_steps",
        "owner_id",
        "project_id",
        "resource_scope",
        "scale_authorization_sha256",
        "task_id",
        "training_material_sha256",
    }
    if set(job) != required_job_keys:
        _fail("job_fields_invalid")
    base = job["base_artifact"]
    if type(base) is not dict or set(base) != {"artifact_ref", "sha256"}:
        _fail("base_artifact_invalid")
    base_sha256 = _require_sha256(base["sha256"], field="base_artifact_sha256")
    base_artifact_ref = _require_bounded_text(
        base["artifact_ref"],
        field="base_artifact_ref",
    )
    candidate_ref = _require_bounded_text(
        job["candidate_artifact_ref"],
        field="candidate_artifact_ref",
    )
    if _looks_like_private_local_path(base_artifact_ref) or _looks_like_private_local_path(
        candidate_ref
    ):
        _fail("artifact_ref_private_path")
    max_steps = job["max_steps"]
    if type(max_steps) is not int or not 1 <= max_steps <= 100_000:
        _fail("max_steps_invalid")
    if step_index >= max_steps:
        _fail("step_index_out_of_bounds")
    for field in (
        "frozen_package_sha256",
        "scale_authorization_sha256",
        "training_material_sha256",
    ):
        _require_sha256(job[field], field=field)
    for field in ("job_id", "owner_id", "project_id", "resource_scope", "task_id"):
        _require_bounded_text(job[field], field=field)
    if job["command_sha256"] != value["command_sha256"]:
        _fail("job_command_mismatch")

    resume_state = value["resume_state"]
    if type(resume_state) is not dict:
        _fail("resume_state_invalid")

    materials_block = value["training_materials"]
    if type(materials_block) is not dict:
        _fail("training_materials_invalid")
    expected_material_keys = {
        "base_artifact_sha256",
        "materials",
        "package_manifest_sha256",
        "required_consumed_materials_sha256",
        "training_material_sha256",
    }
    if set(materials_block) != expected_material_keys:
        _fail("training_material_fields_invalid")
    if materials_block["base_artifact_sha256"] != base_sha256:
        _fail("material_base_identity_mismatch")
    if materials_block["package_manifest_sha256"] != job["frozen_package_sha256"]:
        _fail("material_package_identity_mismatch")
    if materials_block["training_material_sha256"] != job["training_material_sha256"]:
        _fail("material_set_identity_mismatch")
    required_attestation = _require_sha256(
        materials_block["required_consumed_materials_sha256"],
        field="required_consumed_materials_sha256",
    )
    material_values = materials_block["materials"]
    if type(material_values) is not list or not material_values:
        _fail("materials_missing")
    materials = tuple(_parse_material(item) for item in material_values)
    if not any(item.split == "training" for item in materials):
        _fail("training_split_missing")
    if not any(item.split == "validation" for item in materials):
        _fail("validation_split_missing")
    identities = {(item.split, item.artifact_sha256) for item in materials}
    if len(identities) != len(materials):
        _fail("material_identity_duplicate")

    return ParsedRequest(
        step_id=step_id,
        step_index=step_index,
        job_fingerprint=job_fingerprint,
        candidate_artifact_ref=candidate_ref,
        base_artifact_ref=base_artifact_ref,
        base_artifact_sha256=base_sha256,
        max_steps=max_steps,
        required_consumed_materials_sha256=required_attestation,
        materials=materials,
        resume_state=dict(resume_state),
    )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _stable_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_model_directory_file(
    path: Path,
    *,
    expected: os.stat_result | None = None,
) -> tuple[str, int]:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("model_dir entry is not accessible") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("model_dir must contain regular non-linked files only")
    if expected is not None and _stable_stat_identity(before) != _stable_stat_identity(expected):
        raise ValueError("model_dir entry changed before hashing")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("model_dir entry changed before hashing") from exc

    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
        ):
            raise ValueError("model_dir entry changed before hashing")
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_MODEL_DIR_BYTES:
                raise ValueError("model_dir exceeds manifest bounds")
            digest.update(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise ValueError("model_dir entry could not be hashed") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    try:
        current = os.lstat(path)
    except OSError as exc:
        raise ValueError("model_dir entry changed during hashing") from exc
    identity = _stable_stat_identity(opened)
    if (
        total != opened.st_size
        or identity != _stable_stat_identity(after)
        or identity != _stable_stat_identity(current)
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
    ):
        raise ValueError("model_dir entry changed during hashing")
    return digest.hexdigest(), total


def model_directory_manifest_sha256(model_dir: Path) -> str:
    """Hash the exact local tokenizer/config directory used around GGUF loading."""
    root = Path(model_dir)
    if not root.is_absolute():
        raise ValueError("model_dir must be absolute")
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise ValueError("model_dir is not accessible") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise ValueError("model_dir must be a non-linked directory")
    root_identity = _stable_stat_identity(root_stat)

    try:
        paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError as exc:
        raise ValueError("model_dir could not be enumerated") from exc
    relative_paths = [path.relative_to(root).as_posix() for path in paths]

    entries: list[dict[str, object]] = []
    directory_identities: dict[str, tuple[int, int, int, int, int]] = {}
    seen_paths: set[str] = set()
    total_files = 0
    total_bytes = 0
    for path, relative in zip(paths, relative_paths, strict=True):
        folded = relative.casefold()
        if folded in seen_paths:
            raise ValueError("model_dir contains case-fold path collisions")
        seen_paths.add(folded)
        try:
            value = os.lstat(path)
        except OSError as exc:
            raise ValueError("model_dir entry is not accessible") from exc
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            raise ValueError("model_dir must not contain links or reparse points")
        if stat.S_ISDIR(value.st_mode):
            directory_identities[relative] = _stable_stat_identity(value)
            continue
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("model_dir must contain regular files only")
        digest, size_bytes = _hash_model_directory_file(path, expected=value)
        total_files += 1
        total_bytes += size_bytes
        if total_files > _MAX_MODEL_DIR_FILES or total_bytes > _MAX_MODEL_DIR_BYTES:
            raise ValueError("model_dir exceeds manifest bounds")
        entries.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
        )
    if not entries:
        raise ValueError("model_dir must not be empty")

    try:
        root_after = os.lstat(root)
        paths_after = sorted(
            root.rglob("*"),
            key=lambda item: item.relative_to(root).as_posix(),
        )
    except OSError as exc:
        raise ValueError("model_dir changed during manifest") from exc
    if (
        stat.S_ISLNK(root_after.st_mode)
        or _is_reparse(root_after)
        or not stat.S_ISDIR(root_after.st_mode)
        or _stable_stat_identity(root_after) != root_identity
        or [path.relative_to(root).as_posix() for path in paths_after] != relative_paths
    ):
        raise ValueError("model_dir changed during manifest")
    for relative, identity in directory_identities.items():
        try:
            value = os.lstat(root / relative)
        except OSError as exc:
            raise ValueError("model_dir changed during manifest") from exc
        if (
            stat.S_ISLNK(value.st_mode)
            or _is_reparse(value)
            or not stat.S_ISDIR(value.st_mode)
            or _stable_stat_identity(value) != identity
        ):
            raise ValueError("model_dir changed during manifest")
    encoded = json.dumps(
        entries,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"nika-peft-model-dir-v1\x00" + encoded).hexdigest()


def _installed_training_runtime_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution, _ in _TRAINING_RUNTIME_DISTRIBUTIONS:
        try:
            observed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(
                f"training dependency is unavailable: {distribution}"
            ) from exc
        if (
            type(observed) is not str
            or not observed
            or observed != observed.strip()
            or len(observed.encode("utf-8", errors="strict")) > _MAX_RUNTIME_VERSION_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in observed)
        ):
            raise ValueError(
                f"training dependency has invalid version metadata: {distribution}"
            )
        versions[distribution] = observed
    return versions


def _verify_training_runtime_versions() -> None:
    try:
        observed = _installed_training_runtime_versions()
    except (UnicodeEncodeError, ValueError):
        _fail("nika_trainer_runtime_versions_unavailable")
    for distribution, environment_key in _TRAINING_RUNTIME_DISTRIBUTIONS:
        expected = _require_bounded_text(
            os.environ.get(environment_key),
            field=environment_key.lower(),
            max_bytes=_MAX_RUNTIME_VERSION_BYTES,
        )
        if observed[distribution] != expected:
            _fail("nika_trainer_runtime_version_mismatch")


def build_trainer_environment(
    *,
    base_gguf: Path,
    model_dir: Path,
    output_root: Path,
    max_records: int = _MAX_RECORDS_DEFAULT,
    max_sequence_length: int = _MAX_SEQUENCE_LENGTH_DEFAULT,
    learning_rate: float = 2e-4,
    lora_r: int = 8,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    lora_target_modules: tuple[str, ...] = ("q_proj", "v_proj"),
    torch_num_threads: int = 1,
    seed: int = 1729,
) -> dict[str, str]:
    """Build the sterile, execution-plan-bound environment for SubprocessTrainingWorker."""
    base = Path(base_gguf)
    model = Path(model_dir)
    output = Path(output_root)
    if not base.is_absolute() or not model.is_absolute() or not output.is_absolute():
        raise ValueError("trainer paths must be absolute")
    _require_regular_unlinked(base, code="nika_trainer_base_gguf_invalid")
    if base.suffix.casefold() != ".gguf":
        raise ValueError("base_gguf must use the .gguf suffix")
    try:
        model_manifest = model_directory_manifest_sha256(model)
    except ValueError as exc:
        raise ValueError("model_dir is not a canonical local model directory") from exc
    if type(max_records) is not int or not 2 <= max_records <= _MAX_RECORDS_LIMIT:
        raise ValueError("max_records is outside the supported bound")
    if (
        type(max_sequence_length) is not int
        or not 32 <= max_sequence_length <= _MAX_SEQUENCE_LENGTH_LIMIT
    ):
        raise ValueError("max_sequence_length is outside the supported bound")
    for value, name, minimum, maximum in (
        (learning_rate, "learning_rate", 1e-8, 1.0),
        (lora_dropout, "lora_dropout", 0.0, 1.0),
    ):
        if type(value) is not float or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{name} is outside the supported bound")
    if type(lora_r) is not int or not 1 <= lora_r <= 1024:
        raise ValueError("lora_r is outside the supported bound")
    if type(lora_alpha) is not int or not 1 <= lora_alpha <= 65536:
        raise ValueError("lora_alpha is outside the supported bound")
    if type(torch_num_threads) is not int or not 1 <= torch_num_threads <= 256:
        raise ValueError("torch_num_threads is outside the supported bound")
    if type(seed) is not int or not 0 <= seed <= (1 << 31) - 1:
        raise ValueError("seed is outside the supported bound")
    if (
        type(lora_target_modules) is not tuple
        or not lora_target_modules
        or len(lora_target_modules) > 64
        or len(set(lora_target_modules)) != len(lora_target_modules)
        or any(
            type(item) is not str or _TOKEN_RE.fullmatch(item) is None
            for item in lora_target_modules
        )
    ):
        raise ValueError("lora_target_modules is invalid")
    try:
        runtime_versions = _installed_training_runtime_versions()
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("training runtime dependencies are unavailable or invalid") from exc
    output.mkdir(parents=True, exist_ok=True)
    try:
        output_stat = os.lstat(output)
    except OSError as exc:
        raise ValueError("output_root is not accessible") from exc
    if (
        stat.S_ISLNK(output_stat.st_mode)
        or _is_reparse(output_stat)
        or not stat.S_ISDIR(output_stat.st_mode)
    ):
        raise ValueError("output_root must be a non-linked directory")
    environment = {
        "NIKA_TRAINER_BASE_GGUF": os.fspath(base),
        "NIKA_TRAINER_IMPLEMENTATION_SHA256": trainer_implementation_sha256(),
        "NIKA_TRAINER_LEARNING_RATE": format(learning_rate, ".17g"),
        "NIKA_TRAINER_LORA_ALPHA": str(lora_alpha),
        "NIKA_TRAINER_LORA_DROPOUT": format(lora_dropout, ".17g"),
        "NIKA_TRAINER_LORA_R": str(lora_r),
        "NIKA_TRAINER_LORA_TARGET_MODULES": ",".join(lora_target_modules),
        "NIKA_TRAINER_MAX_RECORDS": str(max_records),
        "NIKA_TRAINER_MAX_SEQUENCE_LENGTH": str(max_sequence_length),
        "NIKA_TRAINER_MODEL_DIR": os.fspath(model),
        "NIKA_TRAINER_MODEL_DIR_MANIFEST_SHA256": model_manifest,
        "NIKA_TRAINER_OUTPUT_ROOT": os.fspath(output),
        "NIKA_TRAINER_SEED": str(seed),
        "NIKA_TRAINER_TORCH_NUM_THREADS": str(torch_num_threads),
    }
    for distribution, environment_key in _TRAINING_RUNTIME_DISTRIBUTIONS:
        environment[environment_key] = runtime_versions[distribution]
    return environment


def _require_regular_unlinked(path: Path, *, code: str) -> os.stat_result:
    try:
        value = os.lstat(path)
    except OSError:
        _fail(code)
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISREG(value.st_mode):
        _fail(code)
    return value


def _copy_model_snapshot_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
) -> None:
    before = _require_regular_unlinked(source, code="model_dir_source_changed")
    if before.st_size != expected_size:
        _fail("model_dir_source_changed")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        _fail("model_dir_source_changed")
    total = 0
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != expected_size
        ):
            _fail("model_dir_source_changed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            while True:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size or total > _MAX_MODEL_DIR_BYTES:
                    _fail("model_dir_source_changed")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(fd)
    except PeftTrainerError:
        raise
    except OSError:
        _fail("model_dir_snapshot_failed")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        current = os.lstat(source)
    except OSError:
        _fail("model_dir_source_changed")
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if (
        total != expected_size
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
    ):
        _fail("model_dir_source_changed")


def _model_directory_snapshot(config: TrainerConfig, job_root: Path) -> Path:
    target = job_root / _MODEL_SNAPSHOT_DIR
    if target.exists():
        try:
            digest = model_directory_manifest_sha256(target)
        except ValueError:
            _fail("model_dir_snapshot_invalid")
        if digest != config.model_dir_manifest_sha256:
            _fail("model_dir_snapshot_mismatch")
        return target

    try:
        root_stat = os.lstat(config.model_dir)
    except OSError:
        _fail("model_dir_source_changed")
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        _fail("model_dir_source_changed")
    try:
        paths = sorted(
            config.model_dir.rglob("*"),
            key=lambda item: item.relative_to(config.model_dir).as_posix(),
        )
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".model-snapshot-",
                dir=os.fspath(job_root),
            )
        )
    except OSError:
        _fail("model_dir_snapshot_failed")

    seen_paths: set[str] = set()
    file_count = 0
    total_bytes = 0
    for source in paths:
        relative = source.relative_to(config.model_dir)
        normalized = relative.as_posix()
        folded = normalized.casefold()
        if folded in seen_paths:
            _fail("model_dir_snapshot_path_collision")
        seen_paths.add(folded)
        try:
            value = os.lstat(source)
        except OSError:
            _fail("model_dir_source_changed")
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            _fail("model_dir_snapshot_link_forbidden")
        destination = temporary / relative
        if stat.S_ISDIR(value.st_mode):
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError:
                _fail("model_dir_snapshot_failed")
            continue
        if not stat.S_ISREG(value.st_mode):
            _fail("model_dir_snapshot_invalid")
        file_count += 1
        total_bytes += value.st_size
        if file_count > _MAX_MODEL_DIR_FILES or total_bytes > _MAX_MODEL_DIR_BYTES:
            _fail("model_dir_snapshot_bounds_exceeded")
        _copy_model_snapshot_file(source, destination, expected_size=value.st_size)
    if file_count == 0:
        _fail("model_dir_snapshot_empty")
    try:
        snapshot_digest = model_directory_manifest_sha256(temporary)
    except ValueError:
        _fail("model_dir_snapshot_invalid")
    if snapshot_digest != config.model_dir_manifest_sha256:
        _fail("model_dir_snapshot_mismatch")
    try:
        os.rename(temporary, target)
    except OSError:
        if not target.exists():
            _fail("model_dir_snapshot_publish_failed")
        try:
            existing_digest = model_directory_manifest_sha256(target)
        except ValueError:
            _fail("model_dir_snapshot_publish_failed")
        if existing_digest != config.model_dir_manifest_sha256:
            _fail("model_dir_snapshot_publish_failed")
    return target


def _require_directory_unlinked(path: Path, *, code: str) -> os.stat_result:
    try:
        value = os.lstat(path)
    except OSError:
        _fail(code)
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        _fail(code)
    return value


def _hash_regular_snapshot(path: Path, *, code: str) -> tuple[str, int]:
    before = _require_regular_unlinked(path, code=code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        _fail(code)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _fail(code)
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_CHECKPOINT_BYTES:
                _fail(code)
            digest.update(chunk)
        after = os.fstat(fd)
    except OSError:
        _fail(code)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        current = os.lstat(path)
    except OSError:
        _fail(code)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if (
        total != opened.st_size
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
    ):
        _fail(code)
    return digest.hexdigest(), total


def _parse_jsonl_record(raw_line: bytes) -> TrainingExample:
    if not raw_line or len(raw_line) > _MAX_LINE_BYTES:
        _fail("dataset_record_size_invalid")
    try:
        text = raw_line.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("dataset_record_invalid_json")
    if type(value) is not dict or set(value) != {"prompt", "response"}:
        _fail("dataset_record_fields_invalid")
    prompt = _require_bounded_text(value["prompt"], field="prompt", max_bytes=_MAX_TEXT_BYTES)
    response = _require_bounded_text(value["response"], field="response", max_bytes=_MAX_TEXT_BYTES)
    return TrainingExample(prompt=prompt, response=response)


def _consume_material(
    material: MaterialRequest,
    *,
    max_records: int,
) -> tuple[str, tuple[TrainingExample, ...]]:
    before = _require_regular_unlinked(material.path, code="material_not_regular")
    if before.st_size != material.byte_count:
        _fail("material_size_mismatch")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(material.path, flags)
    except OSError:
        _fail("material_open_failed")
    digest = hashlib.sha256()
    records: list[TrainingExample] = []
    pending = bytearray()
    total = 0
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
            _fail("material_not_regular")
        while True:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, material.byte_count + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > material.byte_count:
                _fail("material_grew_during_read")
            digest.update(chunk)
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > _MAX_LINE_BYTES:
                        _fail("dataset_record_size_invalid")
                    break
                line = bytes(pending[:newline])
                del pending[: newline + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if not line:
                    _fail("dataset_empty_record")
                records.append(_parse_jsonl_record(line))
                if len(records) > max_records:
                    _fail("dataset_record_limit_exceeded")
        if pending:
            records.append(_parse_jsonl_record(bytes(pending)))
            if len(records) > max_records:
                _fail("dataset_record_limit_exceeded")
        after = os.fstat(fd)
    except OSError:
        _fail("material_read_failed")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if total != material.byte_count:
        _fail("material_size_changed")
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _fail("material_changed_during_read")
    actual = digest.hexdigest()
    if actual != material.artifact_sha256:
        _fail("material_digest_mismatch")
    if not records:
        _fail("dataset_empty")
    return actual, tuple(records)


def _consume_materials(request: ParsedRequest, *, max_records: int) -> ConsumedMaterials:
    observations: list[dict[str, object]] = []
    training: list[TrainingExample] = []
    validation: list[TrainingExample] = []
    total_records = 0
    for material in request.materials:
        actual_sha256, records = _consume_material(
            material,
            max_records=max_records - total_records,
        )
        total_records += len(records)
        if total_records > max_records:
            _fail("dataset_record_limit_exceeded")
        observations.append(
            {
                "artifact_sha256": actual_sha256,
                "byte_count": material.byte_count,
                "split": material.split,
            }
        )
        if material.split == "training":
            training.extend(records)
        else:
            validation.extend(records)
    if not training or not validation:
        _fail("dataset_split_empty")
    encoded = json.dumps(
        observations,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    attestation = hashlib.sha256(_MATERIAL_DOMAIN + encoded).hexdigest()
    if attestation != request.required_consumed_materials_sha256:
        _fail("consumed_material_attestation_mismatch")
    return ConsumedMaterials(
        training=tuple(training),
        validation=tuple(validation),
        attestation_sha256=attestation,
    )


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 16:
        _fail(f"{name.lower()}_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        _fail(f"{name.lower()}_invalid")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _fail(f"{name.lower()}_invalid")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        _fail(f"{name.lower()}_invalid")
    return value


def _absolute_env_path(name: str, *, file: bool) -> Path:
    raw = os.environ.get(name)
    if raw is None:
        _fail(f"{name.lower()}_missing")
    text = _require_bounded_text(raw, field=name.lower(), max_bytes=32 * 1024)
    path = Path(text)
    if not path.is_absolute():
        _fail(f"{name.lower()}_not_absolute")
    if file:
        _require_regular_unlinked(path, code=f"{name.lower()}_invalid")
    else:
        try:
            value = os.lstat(path)
        except OSError:
            _fail(f"{name.lower()}_invalid")
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            _fail(f"{name.lower()}_invalid")
    return path


def _read_config() -> TrainerConfig:
    _verify_training_runtime_versions()
    expected_implementation_sha256 = _require_sha256(
        os.environ.get("NIKA_TRAINER_IMPLEMENTATION_SHA256"),
        field="nika_trainer_implementation_sha256",
    )
    if trainer_implementation_sha256() != expected_implementation_sha256:
        _fail("nika_trainer_implementation_mismatch")
    base_gguf = _absolute_env_path("NIKA_TRAINER_BASE_GGUF", file=True)
    if base_gguf.suffix.casefold() != ".gguf":
        _fail("nika_trainer_base_gguf_invalid")
    model_dir = _absolute_env_path("NIKA_TRAINER_MODEL_DIR", file=False)
    model_dir_manifest_sha256 = _require_sha256(
        os.environ.get("NIKA_TRAINER_MODEL_DIR_MANIFEST_SHA256"),
        field="nika_trainer_model_dir_manifest_sha256",
    )
    try:
        live_model_dir_manifest = model_directory_manifest_sha256(model_dir)
    except ValueError:
        _fail("nika_trainer_model_dir_manifest_invalid")
    if live_model_dir_manifest != model_dir_manifest_sha256:
        _fail("nika_trainer_model_dir_manifest_mismatch")
    output_root_raw = os.environ.get("NIKA_TRAINER_OUTPUT_ROOT")
    if output_root_raw is None:
        _fail("nika_trainer_output_root_missing")
    output_root = Path(
        _require_bounded_text(
            output_root_raw,
            field="nika_trainer_output_root",
            max_bytes=32 * 1024,
        )
    )
    if not output_root.is_absolute():
        _fail("nika_trainer_output_root_not_absolute")
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        value = os.lstat(output_root)
    except OSError:
        _fail("nika_trainer_output_root_invalid")
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        _fail("nika_trainer_output_root_invalid")

    raw_targets = os.environ.get("NIKA_TRAINER_LORA_TARGET_MODULES", "q_proj,v_proj")
    target_modules = tuple(item.strip() for item in raw_targets.split(",") if item.strip())
    if not target_modules or len(target_modules) > 64:
        _fail("nika_trainer_lora_target_modules_invalid")
    if any(_TOKEN_RE.fullmatch(item) is None for item in target_modules):
        _fail("nika_trainer_lora_target_modules_invalid")
    if len(set(target_modules)) != len(target_modules):
        _fail("nika_trainer_lora_target_modules_invalid")

    return TrainerConfig(
        base_gguf=base_gguf,
        model_dir=model_dir,
        model_dir_manifest_sha256=model_dir_manifest_sha256,
        output_root=output_root,
        max_records=_env_int(
            "NIKA_TRAINER_MAX_RECORDS",
            _MAX_RECORDS_DEFAULT,
            2,
            _MAX_RECORDS_LIMIT,
        ),
        max_sequence_length=_env_int(
            "NIKA_TRAINER_MAX_SEQUENCE_LENGTH",
            _MAX_SEQUENCE_LENGTH_DEFAULT,
            32,
            _MAX_SEQUENCE_LENGTH_LIMIT,
        ),
        learning_rate=_env_float("NIKA_TRAINER_LEARNING_RATE", 2e-4, 1e-8, 1.0),
        lora_r=_env_int("NIKA_TRAINER_LORA_R", 8, 1, 1024),
        lora_alpha=_env_int("NIKA_TRAINER_LORA_ALPHA", 16, 1, 65536),
        lora_dropout=_env_float("NIKA_TRAINER_LORA_DROPOUT", 0.05, 0.0, 1.0),
        lora_target_modules=target_modules,
        torch_num_threads=_env_int(
            "NIKA_TRAINER_TORCH_NUM_THREADS",
            1,
            1,
            256,
        ),
        seed=_env_int("NIKA_TRAINER_SEED", 1729, 0, (1 << 31) - 1),
    )


def _candidate_key(candidate_artifact_ref: str) -> str:
    encoded = candidate_artifact_ref.encode("utf-8")
    return hashlib.sha256(b"nika-peft-candidate-v1\x00" + encoded).hexdigest()


def candidate_artifact_path(output_root: Path, candidate_artifact_ref: str) -> Path:
    """Derive the physical candidate path from the public logical artifact reference."""
    root = Path(output_root)
    if not root.is_absolute():
        raise ValueError("output_root must be absolute")
    ref = _require_bounded_text(candidate_artifact_ref, field="candidate_artifact_ref")
    return root / _candidate_key(ref) / "candidate" / _CANDIDATE_FILE


def _reserve_candidate_temporary(candidate: Path) -> Path:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{_CANDIDATE_FILE}.",
            suffix=".tmp",
            dir=candidate.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
    except OSError:
        _fail("candidate_publish_failed")
    _require_regular_unlinked(temporary, code="candidate_publish_failed")
    return temporary


def _best_effort_unlink_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        value = os.lstat(path)
    except OSError:
        return
    if (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not _is_reparse(value)
        and (value.st_dev, value.st_ino) == identity
    ):
        try:
            os.unlink(path)
        except OSError:
            pass


def _job_root(config: TrainerConfig, request: ParsedRequest) -> Path:
    return config.output_root / _candidate_key(request.candidate_artifact_ref)


def _ensure_child_directory(parent: Path, name: str, *, code: str) -> Path:
    parent_before = _require_directory_unlinked(parent, code=code)
    child = parent / name
    try:
        child.mkdir(parents=False, exist_ok=True)
    except OSError:
        _fail(code)
    _require_directory_unlinked(child, code=code)
    parent_after = _require_directory_unlinked(parent, code=code)
    if (parent_before.st_dev, parent_before.st_ino) != (
        parent_after.st_dev,
        parent_after.st_ino,
    ):
        _fail(code)
    return child


def _ensure_job_root(config: TrainerConfig, request: ParsedRequest) -> Path:
    return _ensure_child_directory(
        config.output_root,
        _candidate_key(request.candidate_artifact_ref),
        code="job_output_root_failed",
    )


def _copy_verified_base(config: TrainerConfig, request: ParsedRequest, job_root: Path) -> Path:
    source = config.base_gguf
    if _sha256_file(source) != request.base_artifact_sha256:
        _fail("base_gguf_digest_mismatch")
    target_dir = _ensure_child_directory(
        job_root,
        "base",
        code="staged_base_directory_invalid",
    )
    target = target_dir / "base.gguf"
    if target.exists():
        _require_regular_unlinked(target, code="staged_base_invalid")
        if _sha256_file(target) != request.base_artifact_sha256:
            _fail("staged_base_digest_mismatch")
        return target
    temporary = target_dir / ".base.gguf.tmp"
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            while True:
                chunk = src.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if _sha256_file(temporary) != request.base_artifact_sha256:
            _fail("staged_base_digest_mismatch")
        os.replace(temporary, target)
    except FileExistsError:
        _fail("staged_base_conflict")
    except OSError:
        _fail("staged_base_copy_failed")
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return target


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("internal_json_invalid")


def _checkpoint_dir(job_root: Path, step_number: int) -> Path:
    return job_root / "trainer" / f"checkpoint-{step_number}"


def _checkpoint_payload_manifest_sha256(checkpoint: Path) -> str:
    try:
        root_stat = os.lstat(checkpoint)
    except OSError:
        _fail("checkpoint_payload_missing")
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        _fail("checkpoint_payload_invalid")
    try:
        paths = sorted(
            checkpoint.rglob("*"),
            key=lambda item: item.relative_to(checkpoint).as_posix(),
        )
    except OSError:
        _fail("checkpoint_payload_invalid")

    entries: list[dict[str, object]] = []
    casefold_paths: set[str] = set()
    total_bytes = 0
    for path in paths:
        relative = path.relative_to(checkpoint).as_posix()
        if relative in {_CHECKPOINT_MARKER, f".{_CHECKPOINT_MARKER}.tmp"}:
            continue
        try:
            value = os.lstat(path)
        except OSError:
            _fail("checkpoint_payload_invalid")
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            _fail("checkpoint_payload_link_forbidden")
        if stat.S_ISDIR(value.st_mode):
            continue
        if not stat.S_ISREG(value.st_mode):
            _fail("checkpoint_payload_invalid")
        folded = relative.casefold()
        if folded in casefold_paths:
            _fail("checkpoint_payload_path_collision")
        casefold_paths.add(folded)
        digest, size = _hash_regular_snapshot(path, code="checkpoint_payload_changed")
        total_bytes += size
        if len(entries) >= _MAX_CHECKPOINT_FILES or total_bytes > _MAX_CHECKPOINT_BYTES:
            _fail("checkpoint_payload_bounds_exceeded")
        entries.append({"path": relative, "sha256": digest, "size_bytes": size})
    if not entries:
        _fail("checkpoint_payload_empty")
    encoded = _canonical_json_bytes(entries)
    return hashlib.sha256(_CHECKPOINT_PAYLOAD_DOMAIN + encoded).hexdigest()


def _write_checkpoint_marker(
    checkpoint: Path,
    *,
    request: ParsedRequest,
    consumed_sha256: str,
    checkpoint_payload_sha256: str,
) -> str:
    _require_sha256(checkpoint_payload_sha256, field="checkpoint_payload_sha256")
    marker = {
        "checkpoint_payload_sha256": checkpoint_payload_sha256,
        "consumed_materials_sha256": consumed_sha256,
        "job_fingerprint": request.job_fingerprint,
        "schema_version": _SCHEMA_VERSION,
        "step_number": request.step_index + 1,
    }
    encoded = _canonical_json_bytes(marker)
    path = checkpoint / _CHECKPOINT_MARKER
    temporary = checkpoint / f".{_CHECKPOINT_MARKER}.tmp"
    try:
        checkpoint.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except FileExistsError:
        _fail("checkpoint_marker_conflict")
    except OSError:
        _fail("checkpoint_marker_write_failed")
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return hashlib.sha256(encoded).hexdigest()


def _resume_checkpoint(job_root: Path, request: ParsedRequest) -> Path | None:
    state = request.resume_state
    if request.step_index == 0:
        if state:
            _fail("initial_resume_state_invalid")
        return None
    expected = {
        "checkpoint_marker_sha256",
        "checkpoint_payload_sha256",
        "checkpoint_step",
        "job_fingerprint",
        "relative_path",
        "schema_version",
    }
    if set(state) != expected or state.get("schema_version") != _SCHEMA_VERSION:
        _fail("resume_state_invalid")
    if state.get("job_fingerprint") != request.job_fingerprint:
        _fail("resume_job_mismatch")
    checkpoint_step = state.get("checkpoint_step")
    if type(checkpoint_step) is not int or checkpoint_step != request.step_index:
        _fail("resume_step_mismatch")
    relative_path = state.get("relative_path")
    if type(relative_path) is not str:
        _fail("resume_path_invalid")
    expected_path = _checkpoint_dir(job_root, checkpoint_step)
    try:
        candidate = (job_root / relative_path).resolve(strict=True)
        expected_resolved = expected_path.resolve(strict=True)
    except OSError:
        _fail("resume_checkpoint_missing")
    if candidate != expected_resolved:
        _fail("resume_path_mismatch")
    marker_path = candidate / _CHECKPOINT_MARKER
    _require_regular_unlinked(marker_path, code="resume_marker_missing")
    marker_digest = _sha256_file(marker_path)
    if marker_digest != state.get("checkpoint_marker_sha256"):
        _fail("resume_marker_digest_mismatch")
    expected_payload_sha256 = _require_sha256(
        state.get("checkpoint_payload_sha256"),
        field="resume_checkpoint_payload_sha256",
    )
    try:
        marker = json.loads(
            marker_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _fail("resume_marker_invalid")
    expected_marker_keys = {
        "checkpoint_payload_sha256",
        "consumed_materials_sha256",
        "job_fingerprint",
        "schema_version",
        "step_number",
    }
    if (
        type(marker) is not dict
        or set(marker) != expected_marker_keys
        or marker.get("schema_version") != _SCHEMA_VERSION
        or marker.get("job_fingerprint") != request.job_fingerprint
        or marker.get("step_number") != request.step_index
        or marker.get("consumed_materials_sha256")
        != request.required_consumed_materials_sha256
        or marker.get("checkpoint_payload_sha256") != expected_payload_sha256
    ):
        _fail("resume_marker_identity_mismatch")
    if _checkpoint_payload_manifest_sha256(candidate) != expected_payload_sha256:
        _fail("resume_checkpoint_payload_mismatch")
    return candidate


class _TokenizedDataset:
    def __init__(
        self,
        examples: tuple[TrainingExample, ...],
        tokenizer: Any,
        max_length: int,
    ) -> None:
        self._items: list[dict[str, object]] = []
        eos = tokenizer.eos_token or ""
        for example in examples:
            prompt_prefix = f"{example.prompt}\n"
            text = f"{prompt_prefix}{example.response}{eos}"
            prompt_encoded = tokenizer(
                prompt_prefix,
                truncation=True,
                max_length=max_length,
                add_special_tokens=True,
            )
            encoded = tokenizer(
                text,
                truncation=True,
                max_length=max_length,
                add_special_tokens=True,
            )
            prompt_ids = prompt_encoded.get("input_ids")
            input_ids = encoded.get("input_ids")
            attention_mask = encoded.get("attention_mask")
            if (
                type(prompt_ids) is not list
                or type(input_ids) is not list
                or type(attention_mask) is not list
                or len(prompt_ids) >= max_length
                or len(input_ids) <= len(prompt_ids)
                or len(input_ids) != len(attention_mask)
            ):
                _fail("response_tokens_truncated")
            self._items.append(
                {"attention_mask": attention_mask, "input_ids": input_ids}
            )

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> dict[str, object]:
        return self._items[index]


def _import_training_stack() -> tuple[Any, ...]:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from peft import LoraConfig, PeftModel, TaskType, get_peft_model
        from safetensors import safe_open
        from safetensors.torch import save_file as safe_save_file
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            DataCollatorForLanguageModeling,
            Trainer,
            TrainingArguments,
            set_seed,
        )
    except ImportError:
        _fail("training_dependencies_unavailable")
    return (
        torch,
        LoraConfig,
        PeftModel,
        TaskType,
        get_peft_model,
        safe_open,
        safe_save_file,
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
        set_seed,
    )


def _train_one_step(
    request: ParsedRequest,
    config: TrainerConfig,
    consumed: ConsumedMaterials,
) -> tuple[dict[str, object], str | None]:
    (
        torch,
        LoraConfig,
        PeftModel,
        TaskType,
        get_peft_model,
        safe_open,
        safe_save_file,
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
        set_seed,
    ) = _import_training_stack()

    try:
        torch.set_num_threads(config.torch_num_threads)
        torch.use_deterministic_algorithms(True)
    except (RuntimeError, TypeError, ValueError):
        _fail("training_determinism_unavailable")
    set_seed(config.seed)
    job_root = _ensure_job_root(config, request)
    staged_base = _copy_verified_base(config, request, job_root)
    staged_model_dir = _model_directory_snapshot(config, job_root)
    previous_checkpoint = _resume_checkpoint(job_root, request)

    try:
        tokenizer = AutoTokenizer.from_pretrained(
            os.fspath(staged_model_dir),
            gguf_file=os.fspath(staged_base),
            local_files_only=True,
            trust_remote_code=False,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                _fail("tokenizer_padding_unavailable")
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(
            os.fspath(staged_model_dir),
            gguf_file=os.fspath(staged_base),
            local_files_only=True,
            trust_remote_code=False,
            dtype="auto",
        )
        try:
            after_load_manifest = model_directory_manifest_sha256(staged_model_dir)
        except ValueError:
            _fail("model_dir_changed_during_load")
        if after_load_manifest != config.model_dir_manifest_sha256:
            _fail("model_dir_changed_during_load")
        if previous_checkpoint is None:
            lora = LoraConfig(
                r=config.lora_r,
                lora_alpha=config.lora_alpha,
                lora_dropout=config.lora_dropout,
                target_modules=list(config.lora_target_modules),
                task_type=TaskType.CAUSAL_LM,
                bias="none",
            )
            model = get_peft_model(model, lora)
        else:
            adapter_dir = previous_checkpoint / "adapter"
            model = PeftModel.from_pretrained(
                model,
                os.fspath(adapter_dir),
                is_trainable=True,
                local_files_only=True,
            )

        training_dataset = _TokenizedDataset(
            consumed.training,
            tokenizer,
            config.max_sequence_length,
        )
        validation_dataset = _TokenizedDataset(
            consumed.validation,
            tokenizer,
            config.max_sequence_length,
        )
        collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
        trainer_root = _ensure_child_directory(
            job_root,
            "trainer",
            code="trainer_output_directory_invalid",
        )
        _ensure_child_directory(
            trainer_root,
            f"checkpoint-{request.step_index + 1}",
            code="trainer_checkpoint_directory_invalid",
        )
        arguments = TrainingArguments(
            output_dir=os.fspath(trainer_root),
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            learning_rate=config.learning_rate,
            max_steps=request.step_index + 1,
            save_strategy="steps",
            save_steps=1,
            save_total_limit=None,
            eval_strategy="no",
            logging_strategy="no",
            report_to=[],
            seed=config.seed,
            data_seed=config.seed,
            use_cpu=True,
            full_determinism=True,
            dataloader_num_workers=0,
            dataloader_pin_memory=False,
            optim="adamw_torch",
            remove_unused_columns=False,
        )
        trainer = Trainer(
            model=model,
            args=arguments,
            train_dataset=training_dataset,
            eval_dataset=validation_dataset,
            data_collator=collator,
            processing_class=tokenizer,
        )
        trainer.train(
            resume_from_checkpoint=(
                False if previous_checkpoint is None else os.fspath(previous_checkpoint)
            )
        )
        checkpoint = _checkpoint_dir(job_root, request.step_index + 1)
        adapter_dir = checkpoint / "adapter"
        adapter_dir.mkdir(parents=True, exist_ok=True)
        trainer.model.save_pretrained(
            os.fspath(adapter_dir),
            safe_serialization=True,
        )
    except PeftTrainerError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail("training_step_failed")
    finally:
        if "torch" in locals() and hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()

    adapter_file = adapter_dir / _CANDIDATE_FILE
    _require_regular_unlinked(adapter_file, code="adapter_candidate_missing")
    checkpoint_payload_sha256 = _checkpoint_payload_manifest_sha256(checkpoint)
    marker_sha256 = _write_checkpoint_marker(
        checkpoint,
        request=request,
        consumed_sha256=consumed.attestation_sha256,
        checkpoint_payload_sha256=checkpoint_payload_sha256,
    )
    resume_state: dict[str, object] = {
        "checkpoint_marker_sha256": marker_sha256,
        "checkpoint_payload_sha256": checkpoint_payload_sha256,
        "checkpoint_step": request.step_index + 1,
        "job_fingerprint": request.job_fingerprint,
        "relative_path": checkpoint.relative_to(job_root).as_posix(),
        "schema_version": _SCHEMA_VERSION,
    }

    if request.step_index + 1 < request.max_steps:
        return resume_state, None

    candidate = candidate_artifact_path(config.output_root, request.candidate_artifact_ref)
    candidate_parent = _ensure_child_directory(
        job_root,
        "candidate",
        code="candidate_publish_failed",
    )
    if candidate.parent != candidate_parent:
        _fail("candidate_publish_failed")
    if _checkpoint_payload_manifest_sha256(checkpoint) != checkpoint_payload_sha256:
        _fail("checkpoint_payload_changed_before_candidate")
    temporary = _reserve_candidate_temporary(candidate)
    adapter_config = _adapter_config_snapshot(adapter_dir, request, config)
    manifest_json = _candidate_manifest_json(
        request=request,
        config=config,
        consumed=consumed,
        adapter_config=adapter_config,
    )
    temporary_sha256: str | None = None
    temporary_identity: tuple[int, int] | None = None
    published = False
    try:
        with safe_open(os.fspath(adapter_file), framework="pt", device="cpu") as source:
            tensors = {name: source.get_tensor(name) for name in sorted(source.keys())}
        if not tensors:
            _fail("adapter_candidate_empty")
        safe_save_file(
            tensors,
            os.fspath(temporary),
            metadata={"nika_adapter_manifest": manifest_json},
        )
        if _checkpoint_payload_manifest_sha256(checkpoint) != checkpoint_payload_sha256:
            _fail("checkpoint_payload_changed_during_candidate")
        temporary_stat = _require_regular_unlinked(
            temporary,
            code="candidate_publish_failed",
        )
        temporary_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        temporary_sha256, _ = _hash_regular_snapshot(
            temporary,
            code="candidate_publish_failed",
        )
        os.link(temporary, candidate)
        published = True
    except FileExistsError:
        _fail("candidate_publish_conflict")
    except PeftTrainerError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail("candidate_publish_failed")
    finally:
        if not published:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    try:
        os.unlink(temporary)
    except OSError:
        if temporary_identity is not None:
            _best_effort_unlink_identity(candidate, temporary_identity)
        _fail("candidate_publish_cleanup_failed")

    candidate_stat = _require_regular_unlinked(
        candidate,
        code="candidate_publish_digest_mismatch",
    )
    if (
        temporary_identity is None
        or (candidate_stat.st_dev, candidate_stat.st_ino) != temporary_identity
        or candidate_stat.st_nlink != 1
    ):
        if temporary_identity is not None:
            _best_effort_unlink_identity(candidate, temporary_identity)
        _fail("candidate_publish_digest_mismatch")

    candidate_sha256, _ = _hash_regular_snapshot(
        candidate,
        code="candidate_publish_digest_mismatch",
    )
    candidate_after = _require_regular_unlinked(
        candidate,
        code="candidate_publish_digest_mismatch",
    )
    if (
        (candidate_after.st_dev, candidate_after.st_ino) != temporary_identity
        or candidate_after.st_nlink != 1
        or temporary_sha256 is None
        or candidate_sha256 != temporary_sha256
    ):
        _best_effort_unlink_identity(candidate, temporary_identity)
        _fail("candidate_publish_digest_mismatch")
    if _checkpoint_payload_manifest_sha256(checkpoint) != checkpoint_payload_sha256:
        _best_effort_unlink_identity(candidate, temporary_identity)
        _fail("checkpoint_payload_changed_after_candidate")
    return resume_state, candidate_sha256


def _looks_like_private_local_path(value: str) -> bool:
    lowered = value.casefold()
    return (
        value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\\\/]", value) is not None
        or lowered.startswith("file:")
    )


def _reject_private_adapter_config_paths(value: object) -> None:
    if type(value) is str:
        if _looks_like_private_local_path(value):
            _fail("adapter_config_private_path")
        return
    if type(value) is list:
        for item in value:
            _reject_private_adapter_config_paths(item)
        return
    if type(value) is dict:
        for item in value.values():
            _reject_private_adapter_config_paths(item)


def _adapter_config_snapshot(
    path: Path,
    request: ParsedRequest,
    config: TrainerConfig,
) -> dict[str, object]:
    config_path = path / "adapter_config.json"
    _require_regular_unlinked(config_path, code="adapter_config_missing")
    try:
        with config_path.open("rb") as handle:
            raw = handle.read(256 * 1024 + 1)
    except OSError:
        _fail("adapter_config_read_failed")
    if len(raw) > 256 * 1024:
        _fail("adapter_config_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        _bounded_json_tree(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("adapter_config_invalid")
    if type(value) is not dict:
        _fail("adapter_config_invalid")
    expected_targets = set(config.lora_target_modules)
    observed_targets = value.get("target_modules")
    if (
        value.get("r") != config.lora_r
        or value.get("lora_alpha") != config.lora_alpha
        or value.get("lora_dropout") != config.lora_dropout
        or value.get("bias") != "none"
        or value.get("task_type") != "CAUSAL_LM"
        or type(observed_targets) is not list
        or any(type(item) is not str for item in observed_targets)
        or set(observed_targets) != expected_targets
    ):
        _fail("adapter_config_training_plan_mismatch")
    snapshot = dict(value)
    snapshot["base_model_name_or_path"] = request.base_artifact_ref
    snapshot.pop("revision", None)
    _reject_private_adapter_config_paths(snapshot)
    return snapshot


def _candidate_manifest_json(
    *,
    request: ParsedRequest,
    config: TrainerConfig,
    consumed: ConsumedMaterials,
    adapter_config: dict[str, object],
) -> str:
    payload = {
        "adapter_config": adapter_config,
        "base_artifact_ref": request.base_artifact_ref,
        "base_artifact_sha256": request.base_artifact_sha256,
        "candidate_artifact_ref": request.candidate_artifact_ref,
        "consumed_materials_sha256": consumed.attestation_sha256,
        "job_fingerprint": request.job_fingerprint,
        "model_dir_manifest_sha256": config.model_dir_manifest_sha256,
        "schema": "nika-peft-candidate-v1",
        "step_number": request.step_index + 1,
        "trainer_parameters": {
            "learning_rate": config.learning_rate,
            "lora_alpha": config.lora_alpha,
            "lora_dropout": config.lora_dropout,
            "lora_r": config.lora_r,
            "lora_target_modules": list(config.lora_target_modules),
            "max_records": config.max_records,
            "max_sequence_length": config.max_sequence_length,
            "torch_num_threads": config.torch_num_threads,
            "seed": config.seed,
        },
    }
    return _canonical_json_bytes(payload).decode("utf-8")



def _validate_candidate_manifest_payload(
    value: dict[str, object],
) -> dict[str, object]:
    expected = {
        "adapter_config",
        "base_artifact_ref",
        "base_artifact_sha256",
        "candidate_artifact_ref",
        "consumed_materials_sha256",
        "job_fingerprint",
        "model_dir_manifest_sha256",
        "schema",
        "step_number",
        "trainer_parameters",
    }
    if set(value) != expected or value.get("schema") != "nika-peft-candidate-v1":
        _fail("candidate_manifest_invalid")

    base_ref = value["base_artifact_ref"]
    candidate_ref = value["candidate_artifact_ref"]
    if type(base_ref) is not str or type(candidate_ref) is not str:
        _fail("candidate_manifest_invalid")
    try:
        base_ref_bytes = base_ref.encode("utf-8", errors="strict")
        candidate_ref_bytes = candidate_ref.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail("candidate_manifest_invalid")
    if (
        not base_ref
        or not candidate_ref
        or base_ref != base_ref.strip()
        or candidate_ref != candidate_ref.strip()
        or len(base_ref_bytes) > 4096
        or len(candidate_ref_bytes) > 4096
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in base_ref)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate_ref)
        or _looks_like_private_local_path(base_ref)
        or _looks_like_private_local_path(candidate_ref)
    ):
        _fail("candidate_manifest_invalid")
    for field in (
        "base_artifact_sha256",
        "consumed_materials_sha256",
        "job_fingerprint",
        "model_dir_manifest_sha256",
    ):
        if type(value[field]) is not str or _HEX_RE.fullmatch(value[field]) is None:
            _fail("candidate_manifest_invalid")

    step_number = value["step_number"]
    if type(step_number) is not int or not 1 <= step_number <= 100_000:
        _fail("candidate_manifest_invalid")

    parameters = value["trainer_parameters"]
    parameter_keys = {
        "learning_rate",
        "lora_alpha",
        "lora_dropout",
        "lora_r",
        "lora_target_modules",
        "max_records",
        "max_sequence_length",
        "torch_num_threads",
        "seed",
    }
    if type(parameters) is not dict or set(parameters) != parameter_keys:
        _fail("candidate_manifest_invalid")
    learning_rate = parameters["learning_rate"]
    lora_dropout = parameters["lora_dropout"]
    lora_alpha = parameters["lora_alpha"]
    lora_r = parameters["lora_r"]
    max_records = parameters["max_records"]
    max_sequence_length = parameters["max_sequence_length"]
    torch_num_threads = parameters["torch_num_threads"]
    seed = parameters["seed"]
    targets = parameters["lora_target_modules"]
    if (
        type(learning_rate) is not float
        or not math.isfinite(learning_rate)
        or not 1e-8 <= learning_rate <= 1.0
        or type(lora_dropout) is not float
        or not math.isfinite(lora_dropout)
        or not 0.0 <= lora_dropout <= 1.0
        or type(lora_alpha) is not int
        or not 1 <= lora_alpha <= 65_536
        or type(lora_r) is not int
        or not 1 <= lora_r <= 1024
        or type(max_records) is not int
        or not 2 <= max_records <= _MAX_RECORDS_LIMIT
        or type(max_sequence_length) is not int
        or not 32 <= max_sequence_length <= _MAX_SEQUENCE_LENGTH_LIMIT
        or type(torch_num_threads) is not int
        or not 1 <= torch_num_threads <= 256
        or type(seed) is not int
        or not 0 <= seed <= (1 << 31) - 1
        or type(targets) is not list
        or not targets
        or len(targets) > 64
        or any(type(item) is not str or _TOKEN_RE.fullmatch(item) is None for item in targets)
        or len(set(targets)) != len(targets)
    ):
        _fail("candidate_manifest_invalid")

    adapter = value["adapter_config"]
    if type(adapter) is not dict:
        _fail("candidate_manifest_invalid")
    adapter_targets = adapter.get("target_modules")
    if (
        adapter.get("base_model_name_or_path") != base_ref
        or "revision" in adapter
        or adapter.get("r") != lora_r
        or adapter.get("lora_alpha") != lora_alpha
        or adapter.get("lora_dropout") != lora_dropout
        or adapter.get("bias") != "none"
        or adapter.get("task_type") != "CAUSAL_LM"
        or type(adapter_targets) is not list
        or any(type(item) is not str for item in adapter_targets)
        or len(set(adapter_targets)) != len(adapter_targets)
        or set(adapter_targets) != set(targets)
    ):
        _fail("candidate_manifest_invalid")
    _reject_private_adapter_config_paths(adapter)
    return dict(value)


def candidate_adapter_manifest(candidate_path: Path) -> dict[str, object]:
    """Read and strictly validate the self-contained manifest bound into a PEFT candidate."""
    try:
        from safetensors import safe_open
    except ImportError:
        _fail("training_dependencies_unavailable")
    path = Path(candidate_path)
    if not path.is_absolute():
        raise ValueError("candidate_path must be absolute")
    _require_regular_unlinked(path, code="candidate_artifact_invalid")
    try:
        with safe_open(os.fspath(path), framework="pt", device="cpu") as handle:
            metadata = handle.metadata()
    except (OSError, RuntimeError, ValueError):
        _fail("candidate_safetensors_invalid")
    if type(metadata) is not dict or set(metadata) != {"nika_adapter_manifest"}:
        _fail("candidate_manifest_missing")
    raw = metadata["nika_adapter_manifest"]
    if type(raw) is not str or not raw:
        _fail("candidate_manifest_invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        _bounded_json_tree(value)
    except (json.JSONDecodeError, RecursionError, ValueError):
        _fail("candidate_manifest_invalid")
    if type(value) is not dict:
        _fail("candidate_manifest_invalid")
    if _canonical_json_bytes(value).decode("utf-8") != raw:
        _fail("candidate_manifest_not_canonical")
    return _validate_candidate_manifest_payload(value)


def _response(
    request: ParsedRequest,
    consumed: ConsumedMaterials,
    resume_state: dict[str, object],
    candidate_sha256: str | None,
) -> dict[str, object]:
    completed = candidate_sha256 is not None
    return {
        "candidate_sha256": candidate_sha256,
        "completed": completed,
        "consumed_materials_sha256": consumed.attestation_sha256,
        "protocol_version": _PROTOCOL_VERSION,
        "resume_state": resume_state,
        "step_id": request.step_id,
    }


def main() -> int:
    try:
        config = _read_config()
        request = _parse_request(_read_request())
        consumed = _consume_materials(request, max_records=config.max_records)
        resume_state, candidate_sha256 = _train_one_step(request, config, consumed)
        result = _response(request, consumed, resume_state, candidate_sha256)
        sys.stdout.buffer.write(_canonical_json_bytes(result))
        sys.stdout.buffer.flush()
        return 0
    except PeftTrainerError:
        return 2
    except (OSError, RuntimeError, TypeError, ValueError):
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
