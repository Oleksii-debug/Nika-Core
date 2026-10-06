from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.training_peft_worker import (
    candidate_adapter_manifest,
    candidate_artifact_path,
)
from nika_core.training_physical_evaluation_driver import (
    _SCALE_PROGRESSION_OPERATION_TYPE,
    _find_pilot_task,
    _iter_task_idempotency_records,
    load_trusted_scale_progression_proof,
)
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport

_TIER0_ID = "pilot"
_TIER1_ID = "scale-1"
_TIER1_STEPS = 3
_TIER1_CANDIDATE_REF = "models/nika-physical-scale-tier1-adapter"
_EXPERIMENT_ID = "physical-scale-progression-real-proof-v1"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 1024 * 1024


class ProofError(RuntimeError):
    """Observed Windows scale-progression qualification is invalid."""


def _fail(message: str) -> NoReturn:
    raise ProofError(message)


def _strict_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    _fail(f"non-finite JSON constant: {value}")


def _read_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_JSON_BYTES:
            _fail(f"JSON authority has invalid size: {path.name}")
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ProofError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofError(f"invalid JSON authority: {path.name}") from exc
    if type(value) is not dict:
        _fail(f"JSON authority must be an object: {path.name}")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        _fail(f"stale temporary file exists: {temporary.name}")
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_new(path: Path, payload: bytes) -> None:
    if not payload:
        _fail(f"empty evidence payload: {path.name}")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ProofError(f"evidence already exists or cannot be written: {path.name}") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise ProofError(f"cannot hash authority: {path.name}") from exc
    if total <= 0:
        _fail(f"authority is empty: {path.name}")
    return digest.hexdigest(), total


def _candidate_training_digests(
    manifest: dict[str, object],
    *,
    name: str,
) -> tuple[str, str, str]:
    values: list[str] = []
    for field in (
        "previous_adapter_tensors_sha256",
        "trained_adapter_tensors_sha256",
        "tokenization_sha256",
    ):
        value = manifest.get(field)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            _fail(f"{name} lacks canonical {field}")
        values.append(value)
    previous, trained, tokenization = values
    if previous == trained:
        _fail(f"{name} does not prove adapter tensor mutation")
    return previous, trained, tokenization


def _material_limits(
    package: FrozenLearningPackage,
) -> tuple[int, int, int, int]:
    training_records = 0
    training_bytes = 0
    validation_records = 0
    validation_bytes = 0
    for shard in package.shards:
        if shard.split is LearningDataSplit.TRAINING:
            training_records += shard.record_count
            training_bytes += shard.byte_count
        elif shard.split is LearningDataSplit.VALIDATION:
            validation_records += shard.record_count
            validation_bytes += shard.byte_count
    if min(
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
    ) <= 0:
        _fail("frozen package lacks bounded train/validation material")
    return (
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
    )


def _scale_plan_payload(package: FrozenLearningPackage) -> dict[str, object]:
    train_records, train_bytes, validation_records, validation_bytes = (
        _material_limits(package)
    )
    common = {
        "max_training_records": train_records,
        "max_training_bytes": train_bytes,
        "max_validation_records": validation_records,
        "max_validation_bytes": validation_bytes,
    }
    return {
        "plan_id": "physical-real-scale-progression",
        "tiers": [
            {**common, "max_steps": 2, "tier_id": _TIER0_ID},
            {**common, "max_steps": _TIER1_STEPS, "tier_id": _TIER1_ID},
        ],
    }


def prepare_tier0(root: Path) -> None:
    root = root.resolve(strict=True)
    config_path = root / "physical-pilot.json"
    package_path = root / "frozen-package.json"
    config = _read_object(config_path)
    if config.get("schema_version") != 1 or "scale_plan" in config:
        _fail("tier-0 proof config must start from canonical schema-v1 preparation")
    package = FrozenLearningPackage.from_json(
        package_path.read_bytes(),
        expected_manifest_sha256=str(config.get("frozen_package_sha256", "")),
    )
    config["schema_version"] = 2
    config["scale_plan"] = _scale_plan_payload(package)
    _replace_json(config_path, config)
    print(config_path)


def configure_promotion(root: Path) -> None:
    root = root.resolve(strict=True)
    path = root / "physical-evaluation.json"
    config = _read_object(path)
    policy = config.get("policy")
    if type(policy) is not dict:
        _fail("physical evaluation policy is missing")
    if (
        policy.get("primary_metric") != "model_quality_score"
        or policy.get("primary_higher_is_better") is not True
        or policy.get("minimum_replays") != 1
    ):
        _fail("physical evaluation policy is not the canonical real-proof policy")
    policy = dict(policy)
    policy["minimum_improvement"] = 0.0
    config["policy"] = policy
    config["experiment_id"] = _EXPERIMENT_ID
    _replace_json(path, config)
    print(path)


def _pilot_report(root: Path) -> PhysicalTrainingPilotReport:
    try:
        raw = (root / "physical-pilot-report.json").read_text(
            encoding="utf-8",
            errors="strict",
        )
    except OSError as exc:
        raise ProofError("physical pilot report is unavailable") from exc
    return PhysicalTrainingPilotReport.from_json(raw)


def _trusted_progression(
    output_root: Path,
    *,
    workspace_id: str,
) -> object:
    report = _pilot_report(output_root)
    store = SQLiteStore(output_root / "physical-pilot.sqlite3")
    store.initialize()
    task = _find_pilot_task(
        store,
        workspace_id=workspace_id,
        job_id=report.job_id,
    )
    ledger = IdempotencyLedger(store)
    claim: dict[str, object] | None = None
    for record in _iter_task_idempotency_records(
        ledger,
        task_id=task.task_id,
    ):
        if (
            record.status is not IdempotencyStatus.COMPLETED
            or record.operation_type != _SCALE_PROGRESSION_OPERATION_TYPE
        ):
            continue
        result = record.result
        if (
            type(result) is dict
            and result.get("schema")
            == "nika-physical-scale-progression-record-v1"
            and type(result.get("proof")) is dict
        ):
            if claim is not None:
                _fail("exactly one durable scale progression proof is required")
            claim = dict(result["proof"])
    if claim is None:
        _fail("exactly one durable scale progression proof is required")
    proof = load_trusted_scale_progression_proof(
        output_root,
        workspace_id=workspace_id,
        expected_claim=claim,
    )
    if proof.canonical_payload() != claim:
        _fail("restored scale progression proof changed canonical payload")
    return proof


def prepare_tier1(root: Path) -> None:
    root = root.resolve(strict=True)
    tier0_config = _read_object(root / "physical-pilot.json")
    if tier0_config.get("schema_version") != 2:
        _fail("tier-0 scale config must use schema version 2")
    workspace_id = tier0_config.get("workspace_id")
    if type(workspace_id) is not str or not workspace_id:
        _fail("tier-0 workspace identity is invalid")

    tier0_root = root / "run"
    report = _pilot_report(tier0_root)
    evaluation = _read_object(
        tier0_root / "physical-old-new-evaluation-report.json"
    )
    if (
        evaluation.get("experiment_status") != "promoted"
        or evaluation.get("selected_candidate_id")
        != report.candidate_artifact_ref
    ):
        _fail("tier-0 evaluation did not canonically promote the trained challenger")

    proof = _trusted_progression(tier0_root, workspace_id=workspace_id)
    if (
        proof.tier_index != 0
        or proof.candidate_artifact_ref != report.candidate_artifact_ref
        or proof.candidate_sha256 != report.candidate_sha256
    ):
        _fail("durable progression proof does not bind the tier-0 candidate")

    initial_adapter = candidate_artifact_path(
        tier0_root,
        report.candidate_artifact_ref,
    ).resolve(strict=True)
    adapter_sha256, adapter_size = _sha256_file(initial_adapter)
    if (
        adapter_sha256 != report.candidate_sha256
        or adapter_size != report.candidate_byte_count
    ):
        _fail("promoted adapter bytes do not match tier-0 report")

    source_package = FrozenLearningPackage.from_json(
        (root / "frozen-package.json").read_bytes(),
        expected_manifest_sha256=str(
            tier0_config.get("frozen_package_sha256", "")
        ),
    )
    tier1_package = FrozenLearningPackage.freeze(
        package_id=source_package.package_id,
        package_version="2",
        base_artifact_sha256=report.candidate_sha256,
        selection_policy_sha256=source_package.selection_policy_sha256,
        verification_sha256=source_package.verification_sha256,
        evaluation_set_sha256=source_package.evaluation_set_sha256,
        shards=source_package.shards,
    )
    package_path = root / "tier1-frozen-package.json"
    _write_new(package_path, tier1_package.to_json().encode("utf-8"))

    config = dict(tier0_config)
    config.update(
        {
            "schema_version": 3,
            "job_id": "physical-scale-proof-tier1-job",
            "frozen_package_path": str(package_path.resolve(strict=True)),
            "frozen_package_sha256": tier1_package.manifest_sha256,
            "base_artifact_ref": report.candidate_artifact_ref,
            "output_root": str((root / "tier1-run").resolve()),
            "candidate_artifact_ref": _TIER1_CANDIDATE_REF,
            "candidate_descriptor": {
                "model_id": "nika-physical-scale-tier1-adapter",
                "source_reference": "project-internal:physical-scale-progression-proof",
                "license_reference": "project-internal:Nika-Core",
            },
            "scale_tier_id": _TIER1_ID,
            "initial_adapter_path": str(initial_adapter),
            "progression_proof": proof.canonical_payload(),
        }
    )
    path = root / "tier1-physical-pilot.json"
    _write_new(
        path,
        (
            json.dumps(
                config,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    print(path)


def verify(root: Path) -> None:
    root = root.resolve(strict=True)
    source_sha = os.environ.get("NIKA_CANDIDATE_SHA", "")
    if _SHA40_RE.fullmatch(source_sha) is None:
        _fail("NIKA_CANDIDATE_SHA must identify the exact repository head")

    tier0_config = _read_object(root / "physical-pilot.json")
    workspace_id = tier0_config.get("workspace_id")
    if type(workspace_id) is not str or not workspace_id:
        _fail("tier-0 workspace identity is invalid")
    tier0_package_path = root / "frozen-package.json"
    tier0_package = FrozenLearningPackage.from_json(
        tier0_package_path.read_bytes(),
        expected_manifest_sha256=str(
            tier0_config.get("frozen_package_sha256", "")
        ),
    )
    expected_plan = _scale_plan_payload(tier0_package)
    if tier0_config.get("scale_plan") != expected_plan:
        _fail("tier-0 config does not preserve the exact proof scale plan")
    tier0 = _pilot_report(root / "run")
    if (
        tier0.completed_steps != 2
        or tier0.platform != "windows"
        or tier0.frozen_package_sha256 != tier0_package.manifest_sha256
    ):
        _fail("tier-0 evidence is not the real Windows two-step pilot")

    evaluation = _read_object(
        root / "run" / "physical-old-new-evaluation-report.json"
    )
    if (
        evaluation.get("experiment_status") != "promoted"
        or evaluation.get("selected_candidate_id")
        != tier0.candidate_artifact_ref
        or evaluation.get("previous_champion_id")
        != tier0_config.get("base_artifact_ref")
    ):
        _fail("old-vs-new result is not a canonical tier-0 promotion")

    proof = _trusted_progression(root / "run", workspace_id=workspace_id)
    if (
        proof.tier_index != 0
        or proof.candidate_sha256 != tier0.candidate_sha256
        or proof.evaluation_set_sha256
        != tier0_package.evaluation_set_sha256
    ):
        _fail("restored progression authority does not bind tier 0")

    tier1_config = _read_object(root / "tier1-physical-pilot.json")
    if (
        tier1_config.get("schema_version") != 3
        or tier1_config.get("scale_tier_id") != _TIER1_ID
        or tier1_config.get("base_artifact_ref")
        != tier0.candidate_artifact_ref
        or tier1_config.get("scale_plan") != expected_plan
        or tier1_config.get("progression_proof")
        != proof.canonical_payload()
    ):
        _fail("tier-1 config does not consume exact promoted progression authority")

    initial_adapter = Path(str(tier1_config["initial_adapter_path"]))
    initial_sha256, initial_size = _sha256_file(initial_adapter)
    if (
        initial_sha256 != tier0.candidate_sha256
        or initial_size != tier0.candidate_byte_count
    ):
        _fail("tier-1 warm-start adapter changed after promotion")
    initial_manifest = candidate_adapter_manifest(initial_adapter)
    (
        tier0_previous_tensors_sha256,
        initial_tensors_sha256,
        tier0_tokenization_sha256,
    ) = _candidate_training_digests(
        initial_manifest,
        name="tier-0 promoted adapter",
    )
    if (
        tier0_previous_tensors_sha256
        != tier0.previous_adapter_tensors_sha256
        or initial_tensors_sha256 != tier0.trained_adapter_tensors_sha256
    ):
        _fail("tier-0 candidate manifest does not match physical tensor evidence")

    tier1_root = root / "tier1-run"
    tier1 = _pilot_report(tier1_root)
    if (
        tier1.completed_steps != _TIER1_STEPS
        or tier1.platform != "windows"
        or tier1.base_sha256 != tier0.candidate_sha256
        or tier1.candidate_artifact_ref != _TIER1_CANDIDATE_REF
        or tier1.previous_adapter_tensors_sha256
        != initial_tensors_sha256
        or tier1.previous_adapter_tensors_sha256
        == tier1.trained_adapter_tensors_sha256
    ):
        _fail("tier-1 physical report does not prove full-budget warm-start training")

    tier1_package_path = Path(
        str(tier1_config.get("frozen_package_path", ""))
    ).resolve(strict=True)
    expected_tier1_package_path = (
        root / "tier1-frozen-package.json"
    ).resolve(strict=True)
    if tier1_package_path != expected_tier1_package_path:
        _fail("tier-1 config points at an unexpected frozen package")
    tier1_package_bytes = tier1_package_path.read_bytes()
    tier1_package = FrozenLearningPackage.from_json(
        tier1_package_bytes,
        expected_manifest_sha256=tier1.frozen_package_sha256,
    )
    if (
        tier1_config.get("frozen_package_sha256")
        != tier1.frozen_package_sha256
        or tier1_package.base_artifact_sha256
        != tier0.candidate_sha256
        or tier1_package.evaluation_set_sha256
        != tier0_package.evaluation_set_sha256
        or tier1_package.candidate_dataset_sha256
        != tier0_package.candidate_dataset_sha256
    ):
        _fail("tier-1 frozen package does not preserve progression authority")

    store = SQLiteStore(tier1_root / "physical-pilot.sqlite3")
    store.initialize()
    task = _find_pilot_task(
        store,
        workspace_id=workspace_id,
        job_id=tier1.job_id,
    )
    payload = task.payload
    if (
        payload.get("scale_tier_id") != _TIER1_ID
        or payload.get("scale_plan_sha256") != proof.plan_sha256
        or payload.get("progression_proof") != proof.canonical_payload()
        or payload.get("progression_proof_sha256") != proof.proof_sha256
    ):
        _fail("tier-1 durable task does not bind the trusted progression proof")

    candidate = candidate_artifact_path(
        tier1_root,
        tier1.candidate_artifact_ref,
    ).resolve(strict=True)
    candidate_sha256, candidate_size = _sha256_file(candidate)
    if (
        candidate_sha256 != tier1.candidate_sha256
        or candidate_size != tier1.candidate_byte_count
        or candidate_sha256 == tier0.candidate_sha256
    ):
        _fail("tier-1 candidate bytes do not match physical evidence")
    manifest = candidate_adapter_manifest(candidate)
    (
        tier1_previous_tensors_sha256,
        tier1_trained_tensors_sha256,
        tier1_tokenization_sha256,
    ) = _candidate_training_digests(
        manifest,
        name="tier-1 candidate adapter",
    )
    base_gguf_sha256, _ = _sha256_file(root / "base.gguf")
    if (
        manifest.get("schema") != "nika-peft-candidate-v3"
        or manifest.get("base_artifact_ref")
        != tier0.candidate_artifact_ref
        or manifest.get("candidate_artifact_ref") != _TIER1_CANDIDATE_REF
        or manifest.get("base_artifact_sha256") != tier0.candidate_sha256
        or tier1_previous_tensors_sha256 != initial_tensors_sha256
        or tier1_previous_tensors_sha256
        != tier1.previous_adapter_tensors_sha256
        or tier1_trained_tensors_sha256
        != tier1.trained_adapter_tensors_sha256
        or tier1_tokenization_sha256 != tier0_tokenization_sha256
        or manifest.get("foundation_model_sha256") != base_gguf_sha256
    ):
        _fail("tier-1 candidate manifest does not bind warm-start foundation authority")

    staged_assets_path = root / "staged-assets.json"
    staged_assets = _read_object(staged_assets_path)
    staged_assets_sha256, _ = _sha256_file(staged_assets_path)

    evidence = root / "scale-evidence"
    try:
        evidence.mkdir()
    except OSError as exc:
        raise ProofError("scale evidence directory could not be created") from exc
    summary = {
        "schema": "nika-real-physical-scale-progression-proof-v1",
        "source_sha": source_sha,
        "platform": "windows",
        "promotion_policy": "non-regression",
        "minimum_improvement": 0.0,
        "quality_improvement_claimed": False,
        "scale_plan": expected_plan,
        "scale_plan_sha256": proof.plan_sha256,
        "evaluation_set_sha256": proof.evaluation_set_sha256,
        "progression_proof": proof.canonical_payload(),
        "progression_proof_sha256": proof.proof_sha256,
        "staged_assets_sha256": staged_assets_sha256,
        "tier0_frozen_package_sha256": tier0.frozen_package_sha256,
        "tier0_candidate_sha256": tier0.candidate_sha256,
        "tier0_completed_steps": tier0.completed_steps,
        "tier0_previous_adapter_tensors_sha256": (
            tier0_previous_tensors_sha256
        ),
        "tier0_trained_adapter_tensors_sha256": initial_tensors_sha256,
        "tokenization_sha256": tier0_tokenization_sha256,
        "tier1_frozen_package_sha256": tier1.frozen_package_sha256,
        "tier1_candidate_sha256": tier1.candidate_sha256,
        "tier1_completed_steps": tier1.completed_steps,
        "tier1_previous_adapter_tensors_sha256": (
            tier1.previous_adapter_tensors_sha256
        ),
        "tier1_trained_adapter_tensors_sha256": (
            tier1.trained_adapter_tensors_sha256
        ),
    }
    _write_new(
        evidence / "scale-progression-summary.json",
        (_canonical_json(summary) + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-physical-pilot-report.json",
        (tier0.to_json() + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier1-physical-pilot-report.json",
        (tier1.to_json() + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-old-vs-new-evaluation-report.json",
        (_canonical_json(evaluation) + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-frozen-package.json",
        tier0_package.to_json().encode("utf-8"),
    )
    _write_new(
        evidence / "tier1-frozen-package.json",
        tier1_package_bytes,
    )
    _write_new(
        evidence / "staged-assets.json",
        (_canonical_json(staged_assets) + "\n").encode("utf-8"),
    )
    print(_canonical_json(summary))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose and verify an observed Windows PEFT scale progression run."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-tier0", "configure-promotion", "prepare-tier1", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-tier0":
            prepare_tier0(args.root)
        elif args.command == "configure-promotion":
            configure_promotion(args.root)
        elif args.command == "prepare-tier1":
            prepare_tier1(args.root)
        else:
            verify(args.root)
    except (KeyError, OSError, ProofError, RuntimeError, TypeError, ValueError) as exc:
        print(f"physical scale progression proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
