from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.training_peft_worker import candidate_adapter_manifest, candidate_artifact_path
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport

_MODEL_REPOSITORY = "amakhov/tiny-random-llama"
_MODEL_REVISION = "fbf68d33cf68a9d1d4b71b3d098ae82c8c14443b"
_MODEL_LICENSE = "Apache-2.0"
_MODEL_SOURCE_REFERENCE = (
    "https://huggingface.co/amakhov/tiny-random-llama/tree/" + _MODEL_REVISION
)
_MODEL_LICENSE_REFERENCE = "https://www.apache.org/licenses/LICENSE-2.0"
_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
_GGUF_FILE = "gguf/tiny-random-f16.gguf"
_PINNED_ASSET_SHA256 = {
    "model.safetensors": (
        "a4eb5dcdfc71d3a8f297bb1c2a672d3babe04f102480addde293210778805d30"
    ),
    _GGUF_FILE: "1010fc48b2a1880a01fa5e267eb35bf586e3e3ad5539ff5b0e025e4f63616a82",
}
_WORKSPACE_ID = "physical-proof-workspace"
_CANDIDATE_REF = "models/physical-proof-candidate"
_RUNTIME_PACKAGES = (
    "torch",
    "transformers",
    "peft",
    "accelerate",
    "gguf",
    "safetensors",
)
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
_MAX_EVIDENCE_CANDIDATE_BYTES = 64 * 1024 * 1024
_MAX_EVIDENCE_REPORT_BYTES = 64 * 1024
_MAX_EVIDENCE_MANIFEST_BYTES = 64 * 1024
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class ProofError(RuntimeError):
    """The real physical PEFT proof could not be prepared or verified."""


def _fail(message: str) -> NoReturn:
    raise ProofError(message)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _require_candidate_tokenization_sha256(manifest: dict[str, object]) -> str:
    value = manifest.get("tokenization_sha256")
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail("physical proof candidate lacks canonical tokenization evidence")
    return value


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _open_readonly_snapshot(path: Path) -> int:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
        except ImportError as exc:
            raise OSError("Windows evidence snapshot support is unavailable") from exc

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
            _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            return msvcrt.open_osfhandle(
                int(handle),
                os.O_RDONLY | int(getattr(os, "O_BINARY", 0)),
            )
        except (OSError, OverflowError, ValueError):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    return os.open(path, flags)


def _stable_file_bytes(path: Path, *, max_bytes: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            _fail(f"{name} size or file type is invalid")
        descriptor = _open_readonly_snapshot(path)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _fail(f"{name} changed before it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = max_bytes + 1 - total
            if remaining <= 0:
                _fail(f"{name} size is invalid")
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{name} size is invalid")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.lstat(path)
    except ProofError:
        raise
    except OSError as exc:
        raise ProofError(f"{name} could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    identities = (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns),
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns),
    )
    if (
        len(set(identities)) != 1
        or total != before.st_size
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
    ):
        _fail(f"{name} changed while it was being snapshotted")
    if total <= 0 or total > max_bytes:
        _fail(f"{name} size is invalid")
    return b"".join(chunks)


def _write_new_file(path: Path, payload: bytes) -> None:
    if type(payload) is not bytes or not payload:
        _fail("physical proof evidence payload is invalid")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ProofError("physical proof evidence could not be published") from exc


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _download_file(
    relative_path: str,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    url = (
        f"https://huggingface.co/{_MODEL_REPOSITORY}/resolve/"
        f"{_MODEL_REVISION}/{relative_path}?download=true"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nika-Core-physical-PEFT-proof/1"},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    if temporary.exists():
        temporary.unlink()
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    _fail(f"model asset exceeds proof download bound: {relative_path}")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if total <= 0:
            _fail(f"model asset is empty: {relative_path}")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            _fail(f"model asset digest mismatch: {relative_path}")
        os.replace(temporary, destination)
    except ProofError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise ProofError(f"model asset download failed: {relative_path}") from exc
    return {
        "path": relative_path,
        "sha256": actual_sha256,
        "size_bytes": total,
    }


def _dataset_bytes(rows: tuple[tuple[str, str], ...]) -> bytes:
    encoded = "".join(
        _canonical_json({"prompt": prompt, "response": response}) + "\n"
        for prompt, response in rows
    ).encode("utf-8")
    if not encoded:
        _fail("proof dataset must not be empty")
    return encoded


def _learning_shard(
    *,
    split: LearningDataSplit,
    payload: bytes,
    provenance: str,
) -> LearningShard:
    return LearningShard(
        split=split,
        artifact_sha256=_sha256_bytes(payload),
        provenance_sha256=_sha256_bytes(provenance.encode("utf-8")),
        license_evidence_sha256=_sha256_bytes(b"CC0-1.0 proof-only generated text"),
        record_count=payload.count(b"\n"),
        byte_count=len(payload),
    )


def _runtime_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in _RUNTIME_PACKAGES:
        installed = version(package)
        if not installed or installed != installed.strip():
            _fail(f"invalid installed runtime version: {package}")
        result[package] = installed
    return result


def prepare(root: Path, trainer_executable: Path) -> None:
    root = root.resolve()
    trainer_executable = trainer_executable.resolve(strict=True)
    if root.exists():
        _fail("proof root must not already exist")
    if trainer_executable.suffix.casefold() != ".exe":
        _fail("proof trainer executable must be a Windows .exe launcher")
    root.mkdir(parents=True)
    model_dir = root / "model"
    model_dir.mkdir()

    assets: list[dict[str, object]] = []
    for relative_path in _MODEL_FILES:
        assets.append(
            _download_file(
                relative_path,
                model_dir / relative_path,
                expected_sha256=_PINNED_ASSET_SHA256.get(relative_path),
            )
        )
    gguf_path = root / "base.gguf"
    gguf_asset = _download_file(
        _GGUF_FILE,
        gguf_path,
        expected_sha256=_PINNED_ASSET_SHA256[_GGUF_FILE],
    )
    assets.append(gguf_asset)
    gguf_sha256 = str(gguf_asset["sha256"])

    training = _dataset_bytes(
        (
            ("Alpha", "One"),
            ("Beta", "Two"),
            ("Gamma", "Three"),
            ("Delta", "Four"),
        )
    )
    validation = _dataset_bytes((("Validation", "Answer"),))
    blob_store = ContentAddressedBlobStore(root / "blobs")
    blob_store.put_bytes(_WORKSPACE_ID, training)
    blob_store.put_bytes(_WORKSPACE_ID, validation)
    training_shard = _learning_shard(
        split=LearningDataSplit.TRAINING,
        payload=training,
        provenance="Nika Core deterministic physical-proof training corpus v1",
    )
    validation_shard = _learning_shard(
        split=LearningDataSplit.VALIDATION,
        payload=validation,
        provenance="Nika Core deterministic physical-proof validation corpus v1",
    )
    package = FrozenLearningPackage.freeze(
        package_id="physical-proof",
        package_version="1",
        base_artifact_sha256=gguf_sha256,
        selection_policy_sha256=_sha256_bytes(b"fixed physical proof corpus v1"),
        verification_sha256=_sha256_bytes(
            b"repository-native physical pilot verification v1"
        ),
        evaluation_set_sha256=_sha256_bytes(b"held-out proof evaluation identity v1"),
        shards=(training_shard, validation_shard),
    )
    package_path = root / "frozen-package.json"
    package_path.write_text(package.to_json(), encoding="utf-8", newline="")

    runtime_versions = _runtime_versions()
    output_root = root / "run"
    config = {
        "schema_version": 1,
        "workspace_id": _WORKSPACE_ID,
        "project_id": "physical-proof-project",
        "owner_id": "physical-proof-owner",
        "job_id": "physical-proof-job",
        "blob_store_root": str((root / "blobs").resolve()),
        "frozen_package_path": str(package_path.resolve()),
        "frozen_package_sha256": package.manifest_sha256,
        "trainer_executable": str(trainer_executable),
        "base_artifact_ref": "models/amakhov-tiny-random-llama",
        "base_gguf_path": str(gguf_path.resolve()),
        "model_dir": str(model_dir.resolve()),
        "output_root": str(output_root.resolve()),
        "candidate_artifact_ref": _CANDIDATE_REF,
        "candidate_descriptor": {
            "model_id": "nika-physical-proof-adapter",
            "source_reference": _MODEL_SOURCE_REFERENCE,
            "license_reference": _MODEL_LICENSE_REFERENCE,
        },
        "runtime_versions": runtime_versions,
        "resource_budget": {
            "max_cpu_percent": 100,
            "max_memory_percent": 95,
        },
        "trainer_parameters": {
            "max_sequence_length": 32,
            "learning_rate": 0.001,
            "lora_r": 2,
            "lora_alpha": 4,
            "lora_dropout": 0.0,
            "lora_target_modules": ["q_proj", "v_proj"],
            "torch_num_threads": 2,
            "seed": 1729,
        },
    }
    config_path = root / "physical-pilot.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )
    asset_manifest = {
        "license": _MODEL_LICENSE,
        "license_reference": _MODEL_LICENSE_REFERENCE,
        "model_files": assets,
        "repository": _MODEL_REPOSITORY,
        "revision": _MODEL_REVISION,
        "runtime_versions": runtime_versions,
        "source_reference": _MODEL_SOURCE_REFERENCE,
    }
    (root / "staged-assets.json").write_text(
        _canonical_json(asset_manifest) + "\n",
        encoding="utf-8",
        newline="",
    )
    print(config_path)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _verified_asset_manifest(root: Path, raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProofError("physical proof asset manifest is invalid JSON") from exc
    expected_keys = {
        "license",
        "license_reference",
        "model_files",
        "repository",
        "revision",
        "runtime_versions",
        "source_reference",
    }
    if type(value) is not dict or set(value) != expected_keys:
        _fail("physical proof asset manifest fields are invalid")
    if (
        value["repository"] != _MODEL_REPOSITORY
        or value["revision"] != _MODEL_REVISION
        or value["license"] != _MODEL_LICENSE
        or value["source_reference"] != _MODEL_SOURCE_REFERENCE
        or value["license_reference"] != _MODEL_LICENSE_REFERENCE
    ):
        _fail("physical proof asset provenance changed")

    runtime_versions = value["runtime_versions"]
    if (
        type(runtime_versions) is not dict
        or set(runtime_versions) != set(_RUNTIME_PACKAGES)
        or runtime_versions != _runtime_versions()
    ):
        _fail("physical proof runtime version evidence changed")

    raw_files = value["model_files"]
    expected_paths = set(_MODEL_FILES) | {_GGUF_FILE}
    if type(raw_files) is not list or len(raw_files) != len(expected_paths):
        _fail("physical proof asset inventory is invalid")
    observed_paths: set[str] = set()
    for entry in raw_files:
        if type(entry) is not dict or set(entry) != {"path", "sha256", "size_bytes"}:
            _fail("physical proof asset inventory entry is invalid")
        relative = entry["path"]
        digest = entry["sha256"]
        size_bytes = entry["size_bytes"]
        if (
            type(relative) is not str
            or relative not in expected_paths
            or relative in observed_paths
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size_bytes) is not int
            or size_bytes <= 0
            or size_bytes > _MAX_DOWNLOAD_BYTES
        ):
            _fail("physical proof asset inventory entry is invalid")
        observed_paths.add(relative)
        source = root / "base.gguf" if relative == _GGUF_FILE else root / "model" / relative
        payload = _stable_file_bytes(
            source,
            max_bytes=_MAX_DOWNLOAD_BYTES,
            name=f"staged model asset {relative}",
        )
        if len(payload) != size_bytes or _sha256_bytes(payload) != digest:
            _fail(f"staged model asset identity changed: {relative}")
    if observed_paths != expected_paths:
        _fail("physical proof asset inventory is incomplete")
    return value


def _require_open_snapshot_identity(
    path: Path,
    descriptor: int,
    *,
    name: str,
) -> None:
    """Require one held descriptor to remain the exact regular pathname authority."""

    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
    except OSError as exc:
        raise ProofError(f"{name} identity could not be verified") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        _fail(f"{name} identity changed")


def _verified_candidate_evidence_from_snapshot(
    path: Path,
    candidate_bytes: bytes,
) -> tuple[dict[str, object], int, int]:
    """Bind manifest and tensor evidence to one exact admitted candidate snapshot."""

    if type(candidate_bytes) is not bytes or not candidate_bytes:
        _fail("physical proof candidate snapshot is invalid")
    descriptor: int | None = None
    try:
        descriptor = _open_readonly_snapshot(path)
        _require_open_snapshot_identity(
            path,
            descriptor,
            name="physical proof candidate verification snapshot",
        )
        if (
            _stable_file_bytes(
                path,
                max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
                name="physical proof candidate verification snapshot",
            )
            != candidate_bytes
        ):
            _fail("physical proof candidate changed before manifest verification")
        resolved = path.resolve(strict=True)
        manifest = candidate_adapter_manifest(resolved)
        _require_open_snapshot_identity(
            path,
            descriptor,
            name="physical proof candidate verification snapshot",
        )
        if (
            _stable_file_bytes(
                path,
                max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
                name="physical proof candidate verification snapshot",
            )
            != candidate_bytes
        ):
            _fail("physical proof candidate changed during manifest verification")

        from safetensors.torch import load as load_safetensors

        tensors = load_safetensors(candidate_bytes)
        tensor_names = sorted(tensors)
        if not tensor_names:
            _fail("physical proof candidate contains no safetensors tensors")
        total_elements = 0
        for name in tensor_names:
            tensor = tensors[name]
            total_elements += int(tensor.numel())
        if total_elements <= 0:
            _fail("physical proof candidate tensors are empty")
        _require_open_snapshot_identity(
            path,
            descriptor,
            name="physical proof candidate verification snapshot",
        )
        if (
            _stable_file_bytes(
                path,
                max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
                name="physical proof candidate verification snapshot",
            )
            != candidate_bytes
        ):
            _fail("physical proof candidate changed during tensor verification")
    except ProofError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProofError(
            "physical proof candidate evidence could not be verified"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return manifest, len(tensor_names), total_elements


def verify(root: Path) -> None:
    root = root.resolve(strict=True)
    report_path = root / "run" / "physical-pilot-report.json"
    report_bytes = _stable_file_bytes(
        report_path,
        max_bytes=_MAX_EVIDENCE_REPORT_BYTES,
        name="physical proof report",
    )
    raw_report = report_bytes.decode("utf-8", errors="strict")
    report = PhysicalTrainingPilotReport.from_json(raw_report)
    if report.schema_version != 6:
        _fail("physical proof requires a fresh schema-v6 report")
    if report.platform != "windows":
        _fail("physical proof must identify Windows")
    if report.completed_steps != 2:
        _fail("pilot-tier physical proof must complete exactly two steps")
    if report.previous_adapter_tensors_sha256 == report.trained_adapter_tensors_sha256:
        _fail("physical proof did not mutate adapter tensor state")

    candidate = candidate_artifact_path(root / "run", _CANDIDATE_REF)
    if not candidate.is_file():
        _fail("physical proof candidate is missing")
    candidate_bytes = _stable_file_bytes(
        candidate,
        max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
        name="physical proof candidate",
    )
    candidate_sha256 = _sha256_bytes(candidate_bytes)
    candidate_size = len(candidate_bytes)
    if candidate_sha256 != report.candidate_sha256:
        _fail("physical proof candidate digest does not match report")
    if candidate_size != report.candidate_byte_count:
        _fail("physical proof candidate size does not match report")

    evidence_dir = root / "evidence"
    try:
        evidence_dir.mkdir()
    except OSError as exc:
        raise ProofError("physical proof evidence directory could not be created") from exc
    evidence_candidate = evidence_dir / "adapter_model.safetensors"
    _write_new_file(evidence_dir / "physical-pilot-report.json", report_bytes)
    _write_new_file(evidence_candidate, candidate_bytes)

    manifest, candidate_tensor_count, total_elements = (
        _verified_candidate_evidence_from_snapshot(
            evidence_candidate,
            candidate_bytes,
        )
    )
    if manifest.get("schema") != "nika-peft-candidate-v2":
        _fail("pilot proof requires a candidate-v2 tensor-evidence manifest")
    tokenization_sha256 = _require_candidate_tokenization_sha256(manifest)
    if manifest.get("candidate_artifact_ref") != _CANDIDATE_REF:
        _fail("physical proof candidate logical reference changed")
    if (
        manifest.get("previous_adapter_tensors_sha256")
        != report.previous_adapter_tensors_sha256
    ):
        _fail("candidate previous tensor digest does not match report")
    if (
        manifest.get("trained_adapter_tensors_sha256")
        != report.trained_adapter_tensors_sha256
    ):
        _fail("candidate trained tensor digest does not match report")

    assets_bytes = _stable_file_bytes(
        root / "staged-assets.json",
        max_bytes=_MAX_EVIDENCE_MANIFEST_BYTES,
        name="physical proof asset manifest",
    )
    assets = _verified_asset_manifest(root, assets_bytes)
    summary = {
        "asset_revision": assets["revision"],
        "asset_repository": assets["repository"],
        "candidate_byte_count": candidate_size,
        "candidate_sha256": candidate_sha256,
        "candidate_tensor_count": candidate_tensor_count,
        "candidate_tensor_elements": total_elements,
        "completed_steps": report.completed_steps,
        "evidence_sha256": report.evidence_sha256,
        "model_files": assets["model_files"],
        "platform": report.platform,
        "previous_adapter_tensors_sha256": report.previous_adapter_tensors_sha256,
        "runtime_versions": assets["runtime_versions"],
        "schema_version": report.schema_version,
        "trained_adapter_tensors_sha256": report.trained_adapter_tensors_sha256,
        "tokenization_sha256": tokenization_sha256,
    }
    _write_new_file(
        evidence_dir / "physical-proof-summary.json",
        (_canonical_json(summary) + "\n").encode("utf-8"),
    )
    print(report.evidence_sha256)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify the real Windows physical PEFT acceptance run."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True, type=Path)
    prepare_parser.add_argument("--trainer-executable", required=True, type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.root, args.trainer_executable)
        else:
            verify(args.root)
    except (OSError, ProofError, RuntimeError, TypeError, ValueError) as exc:
        print(f"physical PEFT real proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
