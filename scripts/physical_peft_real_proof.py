from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
_GGUF_FILE = "gguf/tiny-random-f16.gguf"
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


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _download_file(relative_path: str, destination: Path) -> dict[str, object]:
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
        os.replace(temporary, destination)
    except ProofError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise ProofError(f"model asset download failed: {relative_path}") from exc
    return {
        "path": relative_path,
        "sha256": digest.hexdigest(),
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
        assets.append(_download_file(relative_path, model_dir / relative_path))
    gguf_path = root / "base.gguf"
    gguf_asset = _download_file(_GGUF_FILE, gguf_path)
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


def verify(root: Path) -> None:
    root = root.resolve(strict=True)
    report_path = root / "run" / "physical-pilot-report.json"
    raw_report = report_path.read_text(encoding="utf-8", errors="strict")
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
    candidate_sha256, candidate_size = _sha256_file(candidate)
    if candidate_size <= 0:
        _fail("physical proof candidate is empty")
    if candidate_sha256 != report.candidate_sha256:
        _fail("physical proof candidate digest does not match report")
    if candidate_size != report.candidate_byte_count:
        _fail("physical proof candidate size does not match report")

    manifest = candidate_adapter_manifest(candidate)
    if manifest.get("schema") != "nika-peft-candidate-v2":
        _fail("pilot proof requires a candidate-v2 tensor-evidence manifest")
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

    from safetensors import safe_open

    with safe_open(os.fspath(candidate), framework="pt", device="cpu") as source:
        tensor_names = sorted(source.keys())
        if not tensor_names:
            _fail("physical proof candidate contains no safetensors tensors")
        total_elements = 0
        for name in tensor_names:
            tensor = source.get_tensor(name)
            total_elements += int(tensor.numel())
        if total_elements <= 0:
            _fail("physical proof candidate tensors are empty")

    assets = json.loads(
        (root / "staged-assets.json").read_text(encoding="utf-8", errors="strict")
    )
    summary = {
        "asset_revision": assets["revision"],
        "asset_repository": assets["repository"],
        "candidate_byte_count": candidate_size,
        "candidate_sha256": candidate_sha256,
        "candidate_tensor_count": len(tensor_names),
        "candidate_tensor_elements": total_elements,
        "completed_steps": report.completed_steps,
        "evidence_sha256": report.evidence_sha256,
        "model_files": assets["model_files"],
        "platform": report.platform,
        "previous_adapter_tensors_sha256": report.previous_adapter_tensors_sha256,
        "runtime_versions": assets["runtime_versions"],
        "schema_version": report.schema_version,
        "trained_adapter_tensors_sha256": report.trained_adapter_tensors_sha256,
    }
    evidence_dir = root / "evidence"
    evidence_dir.mkdir()
    shutil.copyfile(report_path, evidence_dir / "physical-pilot-report.json")
    shutil.copyfile(candidate, evidence_dir / "adapter_model.safetensors")
    (evidence_dir / "physical-proof-summary.json").write_text(
        _canonical_json(summary) + "\n",
        encoding="utf-8",
        newline="",
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
