from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import random
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

_PROTOCOL_VERSION = 3
_CONFIG_SCHEMA_VERSION = 1
_BASE_MANIFEST_SCHEMA_VERSION = 1
_CHECKPOINT_SCHEMA_VERSION = 1
_MATERIAL_ATTESTATION_DOMAIN = b"nika-training-consumed-materials-v1\x00"
_BASE_MANIFEST_DOMAIN = b"nika-peft-base-model-manifest-v1\x00"
_TREE_DIGEST_DOMAIN = b"nika-peft-tree-v1\x00"
_MAX_STDIN_BYTES = 1024 * 1024
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_MODEL_FILES = 4096
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 16384
_MAX_TEXT_BYTES = 1024 * 1024
_MAX_RECORDS = 1_000_000
_READ_CHUNK_BYTES = 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "base_model_root",
        "output_root",
        "torch_version",
        "transformers_version",
        "peft_version",
        "device",
        "seed",
        "torch_num_threads",
        "max_sequence_length",
        "micro_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
        "weight_decay",
        "lora_rank",
        "lora_alpha",
        "lora_dropout",
        "target_modules",
        "prompt_separator",
    }
)
_REQUEST_KEYS = frozenset(
    {
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
)
_JOB_KEYS = frozenset(
    {
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
)
_MATERIAL_SET_KEYS = frozenset(
    {
        "base_artifact_sha256",
        "package_manifest_sha256",
        "required_consumed_materials_sha256",
        "training_material_sha256",
        "materials",
    }
)
_MATERIAL_KEYS = frozenset({"artifact_sha256", "byte_count", "path", "split"})
_RECORD_KEYS = frozenset({"prompt", "response"})
_RESUME_KEYS = frozenset(
    {
        "schema_version",
        "completed_steps",
        "checkpoint_id",
        "checkpoint_manifest_sha256",
        "next_record_index",
    }
)


class WorkerInputError(RuntimeError):
    pass


def _fail(message: str) -> NoReturn:
    raise WorkerInputError(message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _validate_json_tree(value: object) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _fail("JSON value count exceeds the configured bound")
        if depth > _MAX_JSON_DEPTH:
            _fail("JSON nesting exceeds the configured bound")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                _fail("JSON contains a non-finite number")
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail("JSON object contains a non-string key")
                visit(child, depth + 1)
            return
        _fail("JSON contains a non-JSON value")

    visit(value, 0)


def _canonical_json_bytes(value: object) -> bytes:
    _validate_json_tree(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkerInputError("value is not canonical JSON") from exc


def _load_json_bytes(raw: bytes, *, max_bytes: int, label: str) -> object:
    if not raw or len(raw) > max_bytes:
        _fail(f"{label} size is outside the configured bound")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise WorkerInputError(f"{label} is not strict UTF-8 JSON") from exc
    _validate_json_tree(value)
    return value


def _load_json_file(path: Path, *, max_bytes: int, label: str) -> object:
    _require_regular_file(path, label=label)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkerInputError(f"{label} could not be read") from exc
    return _load_json_bytes(raw, max_bytes=max_bytes, label=label)


def _require_sha256(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        _fail(f"{label} must be an exact lowercase SHA-256 digest")
    return value


def _require_text(value: object, *, label: str, max_bytes: int = 4096) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(f"{label} must be non-empty canonical text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(f"{label} must not contain control characters")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkerInputError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > max_bytes:
        _fail(f"{label} exceeds the configured byte limit")
    return value


def _require_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _require_decimal_text(
    value: object,
    *,
    label: str,
    minimum: Decimal,
    maximum: Decimal,
    minimum_inclusive: bool = True,
) -> Decimal:
    text = _require_text(value, label=label, max_bytes=64)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise WorkerInputError(f"{label} must be decimal text") from exc
    if not parsed.is_finite():
        _fail(f"{label} must be finite")
    lower_ok = parsed >= minimum if minimum_inclusive else parsed > minimum
    if not lower_ok or parsed > maximum:
        _fail(f"{label} is outside the configured bound")
    if format(parsed, "f") != text:
        _fail(f"{label} must use canonical plain decimal notation")
    return parsed


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise WorkerInputError(f"{label} is not accessible") from exc


def _reject_link(value: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
        _fail(f"{label} must not be a symbolic link or reparse point")


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    value = _lstat(path, label=label)
    _reject_link(value, label=label)
    if not stat.S_ISREG(value.st_mode):
        _fail(f"{label} must be a regular file")
    return value


def _require_safe_directory(path: Path, *, label: str, create: bool = False) -> Path:
    if not path.is_absolute():
        _fail(f"{label} must be absolute")
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkerInputError(f"{label} could not be created") from exc
    current = path
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for candidate in reversed(chain):
        if not candidate.exists():
            continue
        value = _lstat(candidate, label=label)
        _reject_link(value, label=label)
        if candidate == path and not stat.S_ISDIR(value.st_mode):
            _fail(f"{label} must be a directory")
    return path


def _sha256_file(path: Path, *, expected_bytes: int | None = None) -> tuple[str, int]:
    before = _require_regular_file(path, label="file")
    if expected_bytes is not None and before.st_size != expected_bytes:
        _fail("file size does not match frozen evidence")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb", buffering=0) as stream:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if expected_bytes is not None and total > expected_bytes:
                    _fail("file grew while it was being consumed")
                digest.update(chunk)
    except OSError as exc:
        raise WorkerInputError("file could not be consumed") from exc
    after = _require_regular_file(path, label="file")
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
        _fail("file changed while it was being consumed")
    if expected_bytes is not None and total != expected_bytes:
        _fail("file size changed while it was being consumed")
    return digest.hexdigest(), total


def _read_verified_bytes(path: Path, *, expected_bytes: int) -> tuple[bytes, str]:
    before = _require_regular_file(path, label="training material")
    if before.st_size != expected_bytes:
        _fail("training material size does not match frozen evidence")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb", buffering=0) as stream:
            while True:
                remaining = expected_bytes + 1 - total
                if remaining <= 0:
                    _fail("training material grew while it was being consumed")
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_bytes:
                    _fail("training material grew while it was being consumed")
                digest.update(chunk)
                chunks.append(chunk)
    except OSError as exc:
        raise WorkerInputError("training material could not be consumed") from exc
    after = _require_regular_file(path, label="training material")
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity or total != expected_bytes:
        _fail("training material changed while it was being consumed")
    return b"".join(chunks), digest.hexdigest()


def _relative_posix(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise WorkerInputError("file escaped the authorized root") from exc
    value = relative.as_posix()
    if not value or value.startswith("/") or value in {".", ".."}:
        _fail("relative file path is invalid")
    if any(part in {"", ".", ".."} for part in relative.parts):
        _fail("relative file path is invalid")
    return value


def _enumerate_regular_files(root: Path) -> list[Path]:
    root = _require_safe_directory(root, label="base_model_root")
    files: list[Path] = []
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        value = _lstat(directory_path, label="model directory")
        _reject_link(value, label="model directory")
        for name in dirs:
            child = directory_path / name
            child_value = _lstat(child, label="model directory")
            _reject_link(child_value, label="model directory")
            if not stat.S_ISDIR(child_value.st_mode):
                _fail("model tree contains a non-directory entry")
        for name in names:
            child = directory_path / name
            _require_regular_file(child, label="model file")
            files.append(child)
            if len(files) > _MAX_MODEL_FILES:
                _fail("base model contains too many files")
    files.sort(key=lambda item: _relative_posix(item, root))
    if not files:
        _fail("base model directory is empty")
    return files


def _base_manifest_payload(root: Path) -> dict[str, object]:
    files = []
    for path in _enumerate_regular_files(root):
        sha256, byte_count = _sha256_file(path)
        files.append(
            {
                "byte_count": byte_count,
                "path": _relative_posix(path, root),
                "sha256": sha256,
            }
        )
    return {
        "files": files,
        "schema_version": _BASE_MANIFEST_SCHEMA_VERSION,
    }


def _base_manifest_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(_BASE_MANIFEST_DOMAIN + _canonical_json_bytes(payload)).hexdigest()


def build_base_manifest(root: Path) -> dict[str, object]:
    payload = _base_manifest_payload(root)
    return {
        "manifest": payload,
        "manifest_sha256": _base_manifest_sha256(payload),
    }


def write_base_manifest(root: Path, output: Path) -> str:
    if not output.is_absolute():
        _fail("manifest output path must be absolute")
    if output.exists():
        _fail("manifest output already exists")
    envelope = build_base_manifest(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_json_bytes(envelope) + b"\n")
    return str(envelope["manifest_sha256"])


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    base_model_root: Path
    output_root: Path
    torch_version: str
    transformers_version: str
    peft_version: str
    device: str
    seed: int
    torch_num_threads: int
    max_sequence_length: int
    micro_batch_size: int
    gradient_accumulation_steps: int
    learning_rate: Decimal
    weight_decay: Decimal
    lora_rank: int
    lora_alpha: int
    lora_dropout: Decimal
    target_modules: tuple[str, ...]
    prompt_separator: str


def parse_config(value: object) -> WorkerConfig:
    if type(value) is not dict or frozenset(value) != _CONFIG_KEYS:
        _fail("training config fields are invalid")
    if value["schema_version"] != _CONFIG_SCHEMA_VERSION:
        _fail("unsupported training config schema")
    base_model_root = Path(_require_text(value["base_model_root"], label="base_model_root"))
    output_root = Path(_require_text(value["output_root"], label="output_root"))
    if not base_model_root.is_absolute() or not output_root.is_absolute():
        _fail("training config paths must be absolute")
    if base_model_root == output_root or base_model_root in output_root.parents:
        _fail("output_root must not be inside the immutable base model")
    device = _require_text(value["device"], label="device", max_bytes=16)
    if device != "cpu":
        _fail("this trainer release supports deterministic CPU execution only")
    target_modules_raw = value["target_modules"]
    if type(target_modules_raw) is not list or not 1 <= len(target_modules_raw) <= 64:
        _fail("target_modules must be a non-empty bounded list")
    targets: list[str] = []
    for item in target_modules_raw:
        target = _require_text(item, label="target module", max_bytes=128)
        if target in targets:
            _fail("target_modules must not contain duplicates")
        targets.append(target)
    prompt_separator = value["prompt_separator"]
    if type(prompt_separator) is not str:
        _fail("prompt_separator must be text")
    if len(prompt_separator.encode("utf-8")) > 1024:
        _fail("prompt_separator exceeds the configured byte limit")
    return WorkerConfig(
        base_model_root=base_model_root,
        output_root=output_root,
        torch_version=_require_text(value["torch_version"], label="torch_version", max_bytes=64),
        transformers_version=_require_text(
            value["transformers_version"], label="transformers_version", max_bytes=64
        ),
        peft_version=_require_text(value["peft_version"], label="peft_version", max_bytes=64),
        device=device,
        seed=_require_int(value["seed"], label="seed", minimum=0, maximum=(1 << 31) - 1),
        torch_num_threads=_require_int(
            value["torch_num_threads"],
            label="torch_num_threads",
            minimum=1,
            maximum=64,
        ),
        max_sequence_length=_require_int(
            value["max_sequence_length"],
            label="max_sequence_length",
            minimum=32,
            maximum=8192,
        ),
        micro_batch_size=_require_int(
            value["micro_batch_size"],
            label="micro_batch_size",
            minimum=1,
            maximum=64,
        ),
        gradient_accumulation_steps=_require_int(
            value["gradient_accumulation_steps"],
            label="gradient_accumulation_steps",
            minimum=1,
            maximum=256,
        ),
        learning_rate=_require_decimal_text(
            value["learning_rate"],
            label="learning_rate",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
            minimum_inclusive=False,
        ),
        weight_decay=_require_decimal_text(
            value["weight_decay"],
            label="weight_decay",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        ),
        lora_rank=_require_int(
            value["lora_rank"],
            label="lora_rank",
            minimum=1,
            maximum=1024,
        ),
        lora_alpha=_require_int(
            value["lora_alpha"],
            label="lora_alpha",
            minimum=1,
            maximum=65536,
        ),
        lora_dropout=_require_decimal_text(
            value["lora_dropout"],
            label="lora_dropout",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        ),
        target_modules=tuple(targets),
        prompt_separator=prompt_separator,
    )


def load_config(path: Path) -> WorkerConfig:
    return parse_config(
        _load_json_file(path, max_bytes=_MAX_CONFIG_BYTES, label="training config")
    )


def verify_base_manifest(
    *,
    root: Path,
    envelope: object,
    expected_sha256: str,
) -> str:
    if type(envelope) is not dict or frozenset(envelope) != {"manifest", "manifest_sha256"}:
        _fail("base model manifest envelope fields are invalid")
    manifest = envelope["manifest"]
    declared = _require_sha256(
        envelope["manifest_sha256"],
        label="base model manifest digest",
    )
    if type(manifest) is not dict or frozenset(manifest) != {"files", "schema_version"}:
        _fail("base model manifest fields are invalid")
    if manifest["schema_version"] != _BASE_MANIFEST_SCHEMA_VERSION:
        _fail("unsupported base model manifest schema")
    files = manifest["files"]
    if type(files) is not list or not 1 <= len(files) <= _MAX_MODEL_FILES:
        _fail("base model manifest file count is outside the bound")
    canonical_entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in files:
        if type(item) is not dict or frozenset(item) != {"byte_count", "path", "sha256"}:
            _fail("base model manifest file entry is invalid")
        relative = _require_text(item["path"], label="base model relative path")
        posix = Path(relative)
        if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
            _fail("base model manifest path is invalid")
        if "\\" in relative:
            _fail("base model manifest paths must use POSIX separators")
        if relative in seen:
            _fail("base model manifest paths must be unique")
        seen.add(relative)
        byte_count = _require_int(
            item["byte_count"],
            label="base model byte_count",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        sha256 = _require_sha256(item["sha256"], label="base model file digest")
        canonical_entries.append(
            {"byte_count": byte_count, "path": relative, "sha256": sha256}
        )
    if canonical_entries != sorted(canonical_entries, key=lambda item: str(item["path"])):
        _fail("base model manifest file order is not canonical")
    canonical_manifest = {
        "files": canonical_entries,
        "schema_version": _BASE_MANIFEST_SCHEMA_VERSION,
    }
    observed_manifest_digest = _base_manifest_sha256(canonical_manifest)
    if declared != observed_manifest_digest or expected_sha256 != observed_manifest_digest:
        _fail("base model manifest identity does not match the training job")
    observed_paths = {
        _relative_posix(path, root): path for path in _enumerate_regular_files(root)
    }
    if set(observed_paths) != seen:
        _fail("base model directory contents do not match the frozen manifest")
    for entry in canonical_entries:
        relative = str(entry["path"])
        digest, total = _sha256_file(
            observed_paths[relative],
            expected_bytes=int(entry["byte_count"]),
        )
        if digest != entry["sha256"] or total != entry["byte_count"]:
            _fail("base model file bytes do not match the frozen manifest")
    return observed_manifest_digest


def load_and_verify_base_manifest(
    *,
    path: Path,
    root: Path,
    expected_sha256: str,
) -> str:
    envelope = _load_json_file(
        path,
        max_bytes=_MAX_MANIFEST_BYTES,
        label="base model manifest",
    )
    return verify_base_manifest(
        root=root,
        envelope=envelope,
        expected_sha256=expected_sha256,
    )


def _validate_request(value: object) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != _REQUEST_KEYS:
        _fail("trainer request fields are invalid")
    if value["protocol_version"] != _PROTOCOL_VERSION:
        _fail("unsupported trainer protocol")
    step_index = _require_int(
        value["step_index"],
        label="step_index",
        minimum=0,
        maximum=1_000_000,
    )
    _require_sha256(value["step_id"], label="step_id")
    _require_sha256(value["job_fingerprint"], label="job_fingerprint")
    _require_sha256(value["command_sha256"], label="command_sha256")
    _require_sha256(value["trainer_artifact_id"], label="trainer_artifact_id")
    _require_sha256(value["trainer_sha256"], label="trainer_sha256")
    previous = value["previous_step_id"]
    if previous is not None:
        _require_sha256(previous, label="previous_step_id")
    job = value["job"]
    if type(job) is not dict or frozenset(job) != _JOB_KEYS:
        _fail("trainer job fields are invalid")
    if job["command_sha256"] != value["command_sha256"]:
        _fail("trainer command identity is inconsistent")
    base = job["base_artifact"]
    if type(base) is not dict or frozenset(base) != {"artifact_ref", "sha256"}:
        _fail("base artifact identity is invalid")
    _require_text(base["artifact_ref"], label="base artifact ref")
    _require_sha256(base["sha256"], label="base artifact digest")
    _require_text(job["candidate_artifact_ref"], label="candidate artifact ref")
    _require_sha256(job["frozen_package_sha256"], label="frozen package digest")
    _require_sha256(job["training_material_sha256"], label="training material digest")
    _require_sha256(job["scale_authorization_sha256"], label="scale authorization digest")
    max_steps = _require_int(
        job["max_steps"],
        label="job max_steps",
        minimum=1,
        maximum=1_000_000,
    )
    if step_index >= max_steps:
        _fail("step_index exceeds job max_steps")
    for field in ("job_id", "owner_id", "project_id", "resource_scope", "task_id"):
        _require_text(job[field], label=field)
    materials = value["training_materials"]
    if type(materials) is not dict or frozenset(materials) != _MATERIAL_SET_KEYS:
        _fail("training material request fields are invalid")
    if materials["base_artifact_sha256"] != base["sha256"]:
        _fail("training material base identity is inconsistent")
    if materials["package_manifest_sha256"] != job["frozen_package_sha256"]:
        _fail("training package identity is inconsistent")
    if materials["training_material_sha256"] != job["training_material_sha256"]:
        _fail("training material identity is inconsistent")
    _require_sha256(
        materials["required_consumed_materials_sha256"],
        label="required consumed materials digest",
    )
    raw_items = materials["materials"]
    if type(raw_items) is not list or not raw_items:
        _fail("training material list is empty")
    for item in raw_items:
        if type(item) is not dict or frozenset(item) != _MATERIAL_KEYS:
            _fail("training material entry is invalid")
        _require_sha256(item["artifact_sha256"], label="training shard digest")
        _require_int(
            item["byte_count"],
            label="training shard byte_count",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        material_path = Path(_require_text(item["path"], label="training material path"))
        if not material_path.is_absolute():
            _fail("training material paths must be absolute")
        if item["split"] not in {"training", "validation"}:
            _fail("training material split is invalid")
    resume = value["resume_state"]
    if type(resume) is not dict:
        _fail("trainer resume_state must be an object")
    if step_index == 0:
        if resume:
            _fail("initial trainer step must not carry resume state")
        if previous is not None:
            _fail("initial trainer step must not carry previous_step_id")
    else:
        if frozenset(resume) != _RESUME_KEYS:
            _fail("trainer resume_state fields are invalid")
        if resume["schema_version"] != _CHECKPOINT_SCHEMA_VERSION:
            _fail("unsupported trainer checkpoint schema")
        if resume["completed_steps"] != step_index:
            _fail("trainer resume step does not match request")
        _require_text(resume["checkpoint_id"], label="checkpoint_id", max_bytes=128)
        _require_sha256(
            resume["checkpoint_manifest_sha256"],
            label="checkpoint manifest digest",
        )
        _require_int(
            resume["next_record_index"],
            label="next_record_index",
            minimum=0,
            maximum=(1 << 63) - 1,
        )
        if previous is None:
            _fail("resumed trainer step requires previous_step_id")
    return value


@dataclass(frozen=True, slots=True)
class Example:
    prompt: str
    response: str


def _parse_jsonl_records(raw: bytes, *, split: str) -> list[Example]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkerInputError("training material is not valid UTF-8") from exc
    records: list[Example] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            _fail(f"{split} JSONL contains a blank record")
        try:
            item = json.loads(
                line,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise WorkerInputError(f"{split} JSONL record {line_number} is invalid") from exc
        if type(item) is not dict or frozenset(item) != _RECORD_KEYS:
            _fail(f"{split} JSONL record {line_number} fields are invalid")
        prompt = item["prompt"]
        response = item["response"]
        if type(prompt) is not str or type(response) is not str or not response:
            _fail(f"{split} JSONL record {line_number} text is invalid")
        if (
            len(prompt.encode("utf-8")) > _MAX_TEXT_BYTES
            or len(response.encode("utf-8")) > _MAX_TEXT_BYTES
        ):
            _fail(f"{split} JSONL record {line_number} exceeds the text byte limit")
        records.append(Example(prompt=prompt, response=response))
        if len(records) > _MAX_RECORDS:
            _fail(f"{split} JSONL contains too many records")
    if not records:
        _fail(f"{split} JSONL contains no records")
    return records


def consume_training_materials(
    materials: dict[str, object],
) -> tuple[list[Example], list[Example], str]:
    observations: list[dict[str, object]] = []
    training: list[Example] = []
    validation: list[Example] = []
    raw_items = materials["materials"]
    assert type(raw_items) is list
    for item in raw_items:
        assert type(item) is dict
        path = Path(str(item["path"]))
        expected_bytes = int(item["byte_count"])
        raw, digest = _read_verified_bytes(path, expected_bytes=expected_bytes)
        if digest != item["artifact_sha256"]:
            _fail("training material bytes do not match frozen evidence")
        parsed = _parse_jsonl_records(raw, split=str(item["split"]))
        if item["split"] == "training":
            training.extend(parsed)
        else:
            validation.extend(parsed)
        observations.append(
            {
                "artifact_sha256": item["artifact_sha256"],
                "byte_count": expected_bytes,
                "split": item["split"],
            }
        )
    if not training or not validation:
        _fail("training request requires both training and validation records")
    consumed = hashlib.sha256(
        _MATERIAL_ATTESTATION_DOMAIN + _canonical_json_bytes(observations)
    ).hexdigest()
    required = str(materials["required_consumed_materials_sha256"])
    if consumed != required:
        _fail("consumed training material attestation mismatch")
    return training, validation, consumed


def _tree_entries(root: Path) -> list[dict[str, object]]:
    files = _enumerate_regular_files(root)
    entries: list[dict[str, object]] = []
    for path in files:
        digest, byte_count = _sha256_file(path)
        entries.append(
            {
                "byte_count": byte_count,
                "path": _relative_posix(path, root),
                "sha256": digest,
            }
        )
    return entries


def tree_sha256(root: Path) -> str:
    payload = {"files": _tree_entries(root), "schema_version": 1}
    return hashlib.sha256(_TREE_DIGEST_DOMAIN + _canonical_json_bytes(payload)).hexdigest()


def _write_json_atomic(path: Path, value: object) -> None:
    encoded = _canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _package_version(distribution: str, expected: str) -> None:
    try:
        observed = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise WorkerInputError(f"required training package is unavailable: {distribution}") from exc
    if observed != expected:
        _fail(f"training package version mismatch: {distribution}")


def _verify_backend_versions(config: WorkerConfig) -> None:
    _package_version("torch", config.torch_version)
    _package_version("transformers", config.transformers_version)
    _package_version("peft", config.peft_version)


def _sanitize_adapter_config(adapter_root: Path, *, base_artifact_ref: str) -> None:
    path = adapter_root / "adapter_config.json"
    value = _load_json_file(path, max_bytes=_MAX_CONFIG_BYTES, label="adapter config")
    if type(value) is not dict:
        _fail("adapter config must be an object")
    value["base_model_name_or_path"] = base_artifact_ref
    _write_json_atomic(path, value)


def _checkpoint_root(config: WorkerConfig, job_fingerprint: str) -> Path:
    _require_sha256(job_fingerprint, label="job_fingerprint")
    output = _require_safe_directory(config.output_root, label="output_root", create=True)
    root = output / job_fingerprint
    return _require_safe_directory(root, label="job output root", create=True)


def _checkpoint_id(step_index: int) -> str:
    return f"checkpoint-{step_index + 1:08d}"


def _load_checkpoint(
    *,
    job_root: Path,
    resume_state: dict[str, object],
) -> tuple[Path, int]:
    checkpoint_id = str(resume_state["checkpoint_id"])
    if checkpoint_id != f"checkpoint-{int(resume_state['completed_steps']):08d}":
        _fail("checkpoint_id does not match completed step count")
    checkpoint = job_root / checkpoint_id
    _require_safe_directory(checkpoint, label="checkpoint")
    manifest_path = checkpoint / "checkpoint_manifest.json"
    manifest = _load_json_file(
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
        label="checkpoint manifest",
    )
    if type(manifest) is not dict or frozenset(manifest) != {
        "schema_version",
        "checkpoint_sha256",
        "files",
    }:
        _fail("checkpoint manifest fields are invalid")
    if manifest["schema_version"] != _CHECKPOINT_SCHEMA_VERSION:
        _fail("unsupported checkpoint manifest schema")
    declared = _require_sha256(
        manifest["checkpoint_sha256"],
        label="checkpoint digest",
    )
    if declared != resume_state["checkpoint_manifest_sha256"]:
        _fail("checkpoint resume digest mismatch")
    observed_files = []
    for path in _enumerate_regular_files(checkpoint):
        if path.name == "checkpoint_manifest.json":
            continue
        digest, byte_count = _sha256_file(path)
        observed_files.append(
            {
                "byte_count": byte_count,
                "path": _relative_posix(path, checkpoint),
                "sha256": digest,
            }
        )
    payload = {
        "files": observed_files,
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
    }
    observed = hashlib.sha256(
        b"nika-peft-checkpoint-v1\x00" + _canonical_json_bytes(payload)
    ).hexdigest()
    if observed != declared:
        _fail("checkpoint bytes do not match durable resume evidence")
    return checkpoint, int(resume_state["next_record_index"])


def _write_checkpoint_manifest(checkpoint: Path) -> str:
    files = []
    for path in _enumerate_regular_files(checkpoint):
        if path.name == "checkpoint_manifest.json":
            continue
        digest, byte_count = _sha256_file(path)
        files.append(
            {
                "byte_count": byte_count,
                "path": _relative_posix(path, checkpoint),
                "sha256": digest,
            }
        )
    payload = {"files": files, "schema_version": _CHECKPOINT_SCHEMA_VERSION}
    digest = hashlib.sha256(
        b"nika-peft-checkpoint-v1\x00" + _canonical_json_bytes(payload)
    ).hexdigest()
    _write_json_atomic(
        checkpoint / "checkpoint_manifest.json",
        {
            "checkpoint_sha256": digest,
            **payload,
        },
    )
    return digest


def _encode_example(
    tokenizer: object,
    example: Example,
    config: WorkerConfig,
) -> tuple[list[int], list[int]]:
    eos = getattr(tokenizer, "eos_token", None)
    if type(eos) is not str or not eos:
        _fail("tokenizer must define eos_token")
    prefix = example.prompt + config.prompt_separator
    full = prefix + example.response + eos
    prefix_payload = tokenizer(
        prefix,
        add_special_tokens=True,
        truncation=False,
    )
    full_payload = tokenizer(
        full,
        add_special_tokens=True,
        truncation=True,
        max_length=config.max_sequence_length,
    )
    prefix_ids = prefix_payload.get("input_ids")
    input_ids = full_payload.get("input_ids")
    if type(prefix_ids) is not list or type(input_ids) is not list or not input_ids:
        _fail("tokenizer returned invalid input_ids")
    if len(prefix_ids) >= len(input_ids):
        _fail("max_sequence_length truncates every supervised response token")
    if any(type(item) is not int or item < 0 for item in input_ids):
        _fail("tokenizer returned invalid token IDs")
    labels = list(input_ids)
    for index in range(min(len(prefix_ids), len(labels))):
        labels[index] = -100
    return list(input_ids), labels


def _batch_tensors(
    torch: object,
    tokenizer: object,
    encoded: list[tuple[list[int], list[int]]],
) -> dict[str, object]:
    pad_token_id = getattr(tokenizer, "pad_token_id", None)
    if pad_token_id is None:
        pad_token_id = getattr(tokenizer, "eos_token_id", None)
    if type(pad_token_id) is not int or pad_token_id < 0:
        _fail("tokenizer must define pad_token_id or eos_token_id")
    width = max(len(item[0]) for item in encoded)
    input_batch: list[list[int]] = []
    label_batch: list[list[int]] = []
    attention_batch: list[list[int]] = []
    for input_ids, labels in encoded:
        padding = width - len(input_ids)
        input_batch.append(input_ids + [pad_token_id] * padding)
        label_batch.append(labels + [-100] * padding)
        attention_batch.append([1] * len(input_ids) + [0] * padding)
    return {
        "input_ids": torch.tensor(input_batch, dtype=torch.long),
        "labels": torch.tensor(label_batch, dtype=torch.long),
        "attention_mask": torch.tensor(attention_batch, dtype=torch.long),
    }


def _train_one_step(
    *,
    request: dict[str, object],
    config: WorkerConfig,
    training: list[Example],
    checkpoint: Path | None,
    next_record_index: int,
    temporary_checkpoint: Path,
) -> tuple[int, str]:
    _verify_backend_versions(config)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from peft import LoraConfig, PeftModel, TaskType, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as exc:
        raise WorkerInputError("PEFT training backend could not be imported") from exc

    torch.set_num_threads(config.torch_num_threads)
    random.seed(config.seed + int(request["step_index"]))
    torch.manual_seed(config.seed + int(request["step_index"]))
    torch.use_deterministic_algorithms(True)

    tokenizer = AutoTokenizer.from_pretrained(
        os.fspath(config.base_model_root),
        local_files_only=True,
        trust_remote_code=False,
    )
    base_model = AutoModelForCausalLM.from_pretrained(
        os.fspath(config.base_model_root),
        local_files_only=True,
        trust_remote_code=False,
    )
    base_model.to(device="cpu", dtype=torch.float32)

    if checkpoint is None:
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=config.lora_rank,
            lora_alpha=config.lora_alpha,
            lora_dropout=float(config.lora_dropout),
            target_modules=list(config.target_modules),
            bias="none",
        )
        model = get_peft_model(base_model, peft_config)
    else:
        model = PeftModel.from_pretrained(
            base_model,
            os.fspath(checkpoint / "adapter"),
            is_trainable=True,
        )
    model.to(device="cpu", dtype=torch.float32)
    model.train()
    trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if not trainable:
        _fail("PEFT model exposes no trainable parameters")
    optimizer = torch.optim.AdamW(
        trainable,
        lr=float(config.learning_rate),
        weight_decay=float(config.weight_decay),
    )
    if checkpoint is not None:
        optimizer_state = torch.load(
            checkpoint / "optimizer.pt",
            map_location="cpu",
            weights_only=True,
        )
        optimizer.load_state_dict(optimizer_state)

    optimizer.zero_grad(set_to_none=True)
    cursor = next_record_index % len(training)
    for _ in range(config.gradient_accumulation_steps):
        examples = [
            training[(cursor + offset) % len(training)]
            for offset in range(config.micro_batch_size)
        ]
        cursor = (cursor + config.micro_batch_size) % len(training)
        encoded = [_encode_example(tokenizer, item, config) for item in examples]
        batch = _batch_tensors(torch, tokenizer, encoded)
        output = model(**batch)
        loss = getattr(output, "loss", None)
        if loss is None or not torch.isfinite(loss).item():
            _fail("training step produced a non-finite loss")
        (loss / config.gradient_accumulation_steps).backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)

    adapter_root = temporary_checkpoint / "adapter"
    model.save_pretrained(adapter_root, safe_serialization=True)
    job = request["job"]
    assert type(job) is dict
    base = job["base_artifact"]
    assert type(base) is dict
    _sanitize_adapter_config(
        adapter_root,
        base_artifact_ref=str(base["artifact_ref"]),
    )
    torch.save(optimizer.state_dict(), temporary_checkpoint / "optimizer.pt")
    candidate_sha256 = tree_sha256(adapter_root)
    _write_json_atomic(
        temporary_checkpoint / "step_metadata.json",
        {
            "base_artifact_sha256": base["sha256"],
            "candidate_artifact_ref": job["candidate_artifact_ref"],
            "candidate_sha256": candidate_sha256,
            "completed_steps": int(request["step_index"]) + 1,
            "frozen_package_sha256": job["frozen_package_sha256"],
            "job_fingerprint": request["job_fingerprint"],
            "scale_authorization_sha256": job["scale_authorization_sha256"],
            "training_material_sha256": job["training_material_sha256"],
        },
    )
    return cursor, candidate_sha256


def execute_request(
    request_value: object,
    *,
    config: WorkerConfig,
    base_manifest: object,
    train_step=_train_one_step,
) -> dict[str, object]:
    request = _validate_request(request_value)
    job = request["job"]
    materials = request["training_materials"]
    resume_state = request["resume_state"]
    assert type(job) is dict
    assert type(materials) is dict
    assert type(resume_state) is dict
    base = job["base_artifact"]
    assert type(base) is dict

    _require_safe_directory(config.base_model_root, label="base_model_root")
    _require_safe_directory(config.output_root, label="output_root", create=True)
    verify_base_manifest(
        root=config.base_model_root,
        envelope=base_manifest,
        expected_sha256=str(base["sha256"]),
    )
    training, _validation, consumed_sha256 = consume_training_materials(materials)

    job_root = _checkpoint_root(config, str(request["job_fingerprint"]))
    checkpoint: Path | None = None
    next_record_index = 0
    if int(request["step_index"]) > 0:
        checkpoint, next_record_index = _load_checkpoint(
            job_root=job_root,
            resume_state=resume_state,
        )

    checkpoint_id = _checkpoint_id(int(request["step_index"]))
    final_checkpoint = job_root / checkpoint_id
    if final_checkpoint.exists():
        _fail("target checkpoint already exists")
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-step-", dir=job_root))
    try:
        cursor, candidate_sha256 = train_step(
            request=request,
            config=config,
            training=training,
            checkpoint=checkpoint,
            next_record_index=next_record_index,
            temporary_checkpoint=temporary,
        )
        checkpoint_sha256 = _write_checkpoint_manifest(temporary)
        temporary.rename(final_checkpoint)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    completed = int(request["step_index"]) + 1 >= int(job["max_steps"])
    if completed:
        _write_json_atomic(
            job_root / "candidate.json",
            {
                "artifact_ref": job["candidate_artifact_ref"],
                "checkpoint_id": checkpoint_id,
                "relative_path": f"{checkpoint_id}/adapter",
                "sha256": candidate_sha256,
            },
        )
    return {
        "candidate_sha256": candidate_sha256 if completed else None,
        "completed": completed,
        "consumed_materials_sha256": consumed_sha256,
        "protocol_version": _PROTOCOL_VERSION,
        "resume_state": {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "completed_steps": int(request["step_index"]) + 1,
            "checkpoint_id": checkpoint_id,
            "checkpoint_manifest_sha256": checkpoint_sha256,
            "next_record_index": cursor,
        },
        "step_id": request["step_id"],
    }


def _read_stdin_bounded() -> bytes:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        _fail("trainer request exceeds the configured byte limit")
    return raw


def _production_main(config_path: Path, manifest_path: Path) -> int:
    config = load_config(config_path)
    request = _load_json_bytes(
        _read_stdin_bounded(),
        max_bytes=_MAX_STDIN_BYTES,
        label="trainer request",
    )
    validated = _validate_request(request)
    envelope = _load_json_file(
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
        label="base model manifest",
    )
    response = execute_request(
        validated,
        config=config,
        base_manifest=envelope,
    )
    sys.stdout.buffer.write(_canonical_json_bytes(response))
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "--build-base-manifest":
            if len(arguments) != 3:
                _fail("--build-base-manifest requires ROOT and OUTPUT")
            root = Path(arguments[1])
            output = Path(arguments[2])
            if not root.is_absolute() or not output.is_absolute():
                _fail("manifest build paths must be absolute")
            digest = write_base_manifest(root, output)
            sys.stdout.write(digest + "\n")
            return 0
        if len(arguments) != 2:
            _fail("production mode requires absolute CONFIG and BASE_MANIFEST paths")
        config_path = Path(arguments[0])
        manifest_path = Path(arguments[1])
        if not config_path.is_absolute() or not manifest_path.is_absolute():
            _fail("production paths must be absolute")
        return _production_main(config_path, manifest_path)
    except WorkerInputError as exc:
        sys.stderr.write(f"nika_peft_trainer_error:{exc}\n")
        return 2
    except Exception:
        sys.stderr.write("nika_peft_trainer_error:backend_failure\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
