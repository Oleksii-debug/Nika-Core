from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.training_physical_evaluation_driver as driver
import nika_core.training_scale as training_scale
from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ExperimentStatus,
    MetricObservation,
    SQLiteExperimentRepository,
)
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import EvaluationPurpose
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "evaluation-workspace",
        "project_id": "evaluation-project",
        "owner_id": "evaluation-owner",
        "physical_pilot_output_root": str(tmp_path / "pilot output"),
        "frozen_package_path": str(tmp_path / "frozen-package.json"),
        "base_artifact_ref": "models/base",
        "base_model_path": str(tmp_path / "base model.gguf"),
        "candidate_model_path": str(
            tmp_path / "pilot output" / "candidate" / "adapter_model.safetensors"
        ),
        "base_model": {
            "provider_id": "local-base",
            "model_id": "base-model",
            "model_version": "v1",
            "source_reference": "https://example.com/models/base",
            "license_reference": "https://example.com/licenses/base",
            "capabilities": ["text"],
        },
        "candidate_model": {
            "model_id": "nika-pilot-adapter",
            "source_reference": "https://example.com/models/base",
            "license_reference": "https://example.com/licenses/base",
        },
        "evaluator": {
            "executable": str(tmp_path / "evaluator.exe"),
            "command_files": [str(tmp_path / "evaluate.py")],
            "switches": ["--strict"],
            "provenance_ref": "https://example.com/evaluator/source",
            "license_ref": "https://example.com/evaluator/license",
        },
        "evaluation_set_path": str(tmp_path / "held-out.json"),
        "experiment_id": "physical-old-new-job-1",
        "permission_fingerprint": "evaluation-read-only",
        "benchmark": {
            "timeout_seconds": 60.0,
            "temperature": 0.0,
            "scorer_id": "exact-match-nfc-v1",
        },
        "policy": {
            "primary_metric": "model_quality_score",
            "minimum_improvement": 0.0,
            "minimum_replays": 1,
            "primary_higher_is_better": True,
            "guardrails": [
                {
                    "metric": "model_task_pass",
                    "higher_is_better": True,
                    "max_regression": 0.0,
                }
            ],
        },
    }


def _config(tmp_path: Path) -> driver.PhysicalEvaluationConfig:
    raw = json.dumps(_payload(tmp_path), ensure_ascii=False, sort_keys=True)
    return driver.PhysicalEvaluationConfig.from_json(raw)


def _evaluation_payload(*, purpose: str = "held_out") -> dict[str, object]:
    return {
        "evaluation_set_id": "held-out-physical",
        "version": "v1",
        "provenance_ref": "dataset:held-out-physical",
        "license_ref": "license:held-out-physical",
        "purpose": purpose,
        "privacy": "private",
        "cases": [
            {
                "case_id": "case-one",
                "messages": [{"role": "user", "content": "секретне питання"}],
                "expected_text": "відповідь",
                "pass_score": 1.0,
                "weight": 1.0,
            }
        ],
    }


def _candidate_config() -> driver.CandidateModelConfig:
    return driver.CandidateModelConfig(
        model_id="nika-pilot-adapter",
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
    )


def _candidate_descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="training-runtime",
        model_id="nika-pilot-adapter",
        model_version="9" * 64,
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256="9" * 64,
        size_bytes=123,
        capabilities=("text",),
    )


def _scale_task_payload(
    *,
    kind: str = "physical_peft_pilot",
    proof_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "job_id": "pilot-job",
        "kind": kind,
        "progression_proof_sha256": proof_sha256,
        "scale_plan_sha256": "8" * 64,
        "scale_tier_id": "pilot" if kind == "physical_peft_pilot" else "small",
    }


def _scale_plan_payload() -> dict[str, object]:
    return {
        "evaluation_set_sha256": "e" * 64,
        "plan_id": "physical-scale",
        "schema_version": 1,
        "tiers": [
            {
                "max_steps": 2,
                "max_training_bytes": 4096,
                "max_training_records": 10,
                "max_validation_bytes": 4096,
                "max_validation_records": 10,
                "tier_id": "pilot",
            },
            {
                "max_steps": 8,
                "max_training_bytes": 65536,
                "max_training_records": 100,
                "max_validation_bytes": 8192,
                "max_validation_records": 20,
                "tier_id": "small",
            },
        ],
    }


def _scale_task_payload_with_plan(
    *,
    kind: str = "physical_peft_pilot",
    proof_sha256: str | None = None,
) -> dict[str, object]:
    plan_payload = _scale_plan_payload()
    plan = driver.TrainingScalePlan.from_canonical_payload(plan_payload)
    return {
        **_scale_task_payload(kind=kind, proof_sha256=proof_sha256),
        "scale_plan": plan_payload,
        "scale_plan_sha256": plan.plan_sha256,
    }


def _scale_task_payload_with_chain(
    *,
    kind: str = "physical_peft_pilot",
    proof: driver.TrainingScaleProgressionProof | None = None,
) -> dict[str, object]:
    payload = _scale_task_payload_with_plan(
        kind=kind,
        proof_sha256=None if proof is None else proof.proof_sha256,
    )
    payload["progression_proof"] = (
        None if proof is None else proof.canonical_payload()
    )
    return payload


def _pilot_report(
    *,
    descriptor: ModelArtifactDescriptor | None = None,
) -> PhysicalTrainingPilotReport:
    descriptor = descriptor or _candidate_descriptor()
    return PhysicalTrainingPilotReport(
        job_id="pilot-job",
        base_sha256="1" * 64,
        frozen_package_sha256="2" * 64,
        training_material_sha256="3" * 64,
        scale_authorization_sha256="4" * 64,
        execution_plan_sha256="5" * 64,
        job_fingerprint="6" * 64,
        trainer_job_fingerprint="7" * 64,
        paused_checkpoint_id="pause-checkpoint",
        restart_checkpoint_id="restart-checkpoint",
        completed_checkpoint_id="complete-checkpoint",
        candidate_artifact_ref="models/pilot-candidate",
        candidate_descriptor_sha256=descriptor.descriptor_digest,
        candidate_registry_key=descriptor.registry_key,
        candidate_sha256="9" * 64,
        candidate_byte_count=123,
        candidate_manifest_sha256="a" * 64,
        consumed_materials_sha256="b" * 64,
        model_dir_manifest_sha256="c" * 64,
        previous_adapter_tensors_sha256="1" * 64,
        trained_adapter_tensors_sha256="2" * 64,
        trainer_artifact_id="d" * 64,
        trainer_deployment_sha256="e" * 64,
        trainer_implementation_sha256="f" * 64,
        training_runtime_manifest_sha256="0" * 64,
        completed_steps=2,
    )


def test_config_parses_strict_local_authorities(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.workspace_id == "evaluation-workspace"
    assert config.base_model.provider_id == "local-base"
    assert config.candidate_model.model_id == "nika-pilot-adapter"
    assert config.evaluator.switches == ("--strict",)
    assert config.benchmark.scorer_id == "exact-match-nfc-v1"
    assert config.policy.primary_metric == "model_quality_score"


def test_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["unexpected"] = True

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="fields are invalid"):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))


def test_config_rejects_duplicate_json_field(tmp_path: Path) -> None:
    body = json.dumps(_payload(tmp_path))
    duplicated = body[:-1] + ',"owner_id":"other"}'

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="invalid JSON"):
        driver.PhysicalEvaluationConfig.from_json(duplicated)


@pytest.mark.parametrize(
    "switch",
    ("script.py", "../escape", "--bad=value", "/absolute", "модель"),
)
def test_config_rejects_unbound_evaluator_arguments(
    tmp_path: Path,
    switch: str,
) -> None:
    payload = _payload(tmp_path)
    evaluator = payload["evaluator"]
    assert isinstance(evaluator, dict)
    evaluator["switches"] = [switch]

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="simple --option"):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provenance_ref", r"C:\\private\\evaluator.exe"),
        ("license_ref", "https://example.com/license?token=secret"),
        ("provenance_ref", "env:EVALUATOR_SECRET"),
    ),
)
def test_config_rejects_private_or_secret_evaluator_provenance(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _payload(tmp_path)
    evaluator = payload["evaluator"]
    assert isinstance(evaluator, dict)
    evaluator[field] = value

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="public and secret-free",
    ):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))

def test_evaluation_set_parser_preserves_held_out_identity() -> None:
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )

    assert evaluation.purpose is EvaluationPurpose.HELD_OUT
    assert evaluation.cases[0].messages[0].content == "секретне питання"
    assert len(evaluation.content_sha256) == 64


def test_evaluation_set_parser_rejects_development_data() -> None:
    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="purpose=held_out",
    ):
        driver._evaluation_set_from_json(
            json.dumps(_evaluation_payload(purpose="development"))
        )


def test_evaluation_set_parser_rejects_duplicate_case_field() -> None:
    raw = json.dumps(_evaluation_payload(), ensure_ascii=False)
    raw = raw.replace(
        '"case_id": "case-one"',
        '"case_id": "case-one", "case_id": "case-two"',
    )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="invalid JSON"):
        driver._evaluation_set_from_json(raw)


def test_non_windows_gate_precedes_filesystem_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: False)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="must execute on Windows",
    ):
        driver.run_physical_evaluation_from_config(config)

    assert not config.physical_pilot_output_root.exists()


def test_find_pilot_task_requires_exact_unique_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    expected = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
    )
    TaskQueue(store).create(
        workspace_id="other-workspace",
        agent_id="physical-peft-pilot",
        payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
    )

    actual = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )

    assert actual.task_id == expected.task_id


@pytest.mark.parametrize(
    ("kind", "proof_sha256"),
    (
        ("physical_peft_pilot", None),
        ("physical_peft_scale_tier", "9" * 64),
    ),
)
def test_find_pilot_task_accepts_scale_aware_training_identity(
    tmp_path: Path,
    kind: str,
    proof_sha256: str | None,
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    expected = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload=_scale_task_payload(kind=kind, proof_sha256=proof_sha256),
    )

    actual = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )

    assert actual.task_id == expected.task_id


@pytest.mark.parametrize(
    ("kind", "proof_sha256"),
    (
        ("physical_peft_pilot", None),
        ("physical_peft_scale_tier", "9" * 64),
    ),
)
def test_find_pilot_task_accepts_plan_bound_scale_identity(
    tmp_path: Path,
    kind: str,
    proof_sha256: str | None,
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    expected = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload=_scale_task_payload_with_plan(
            kind=kind,
            proof_sha256=proof_sha256,
        ),
    )

    actual = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )

    assert actual.task_id == expected.task_id


def test_find_pilot_task_accepts_chained_higher_tier_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    plan = driver.TrainingScalePlan.from_canonical_payload(_scale_plan_payload())
    proof = training_scale._build_progression_proof(
        plan_sha256=plan.plan_sha256,
        tier_index=0,
        authorization_sha256="2" * 64,
        job_id="pilot-job",
        job_fingerprint="3" * 64,
        base_artifact_ref="models/base",
        base_sha256="4" * 64,
        candidate_artifact_ref="models/pilot-candidate",
        candidate_sha256="5" * 64,
        frozen_package_sha256="6" * 64,
        training_material_sha256="7" * 64,
        execution_plan_sha256="8" * 64,
        comparison_evidence_sha256="9" * 64,
        evaluation_set_sha256=plan.evaluation_set_sha256,
    )
    expected = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload={
            **_scale_task_payload_with_chain(
                kind="physical_peft_scale_tier",
                proof=proof,
            ),
            "scale_tier_id": "small",
        },
    )

    actual = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )

    assert actual.task_id == expected.task_id


def test_find_pilot_task_rejects_chained_proof_digest_mismatch(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    plan = driver.TrainingScalePlan.from_canonical_payload(_scale_plan_payload())
    proof = training_scale._build_progression_proof(
        plan_sha256=plan.plan_sha256,
        tier_index=0,
        authorization_sha256="2" * 64,
        job_id="pilot-job",
        job_fingerprint="3" * 64,
        base_artifact_ref="models/base",
        base_sha256="4" * 64,
        candidate_artifact_ref="models/pilot-candidate",
        candidate_sha256="5" * 64,
        frozen_package_sha256="6" * 64,
        training_material_sha256="7" * 64,
        execution_plan_sha256="8" * 64,
        comparison_evidence_sha256="9" * 64,
        evaluation_set_sha256=plan.evaluation_set_sha256,
    )
    payload = {
        **_scale_task_payload_with_chain(
            kind="physical_peft_scale_tier",
            proof=proof,
        ),
        "progression_proof_sha256": "f" * 64,
        "scale_tier_id": "small",
    }
    TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload=payload,
    )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="exactly one"):
        driver._find_pilot_task(
            store,
            workspace_id="evaluation-workspace",
            job_id="pilot-job",
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            **_scale_task_payload_with_plan(),
            "scale_plan_sha256": "f" * 64,
        },
        {
            **_scale_task_payload_with_plan(),
            "scale_tier_id": "small",
        },
        {
            **_scale_task_payload_with_plan(
                kind="physical_peft_scale_tier",
                proof_sha256="9" * 64,
            ),
            "scale_tier_id": "pilot",
        },
    ),
)
def test_find_pilot_task_rejects_inconsistent_plan_bound_identity(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload=payload,
    )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="exactly one"):
        driver._find_pilot_task(
            store,
            workspace_id="evaluation-workspace",
            job_id="pilot-job",
        )


@pytest.mark.parametrize(
    "payload",
    (
        _scale_task_payload(
            kind="physical_peft_pilot",
            proof_sha256="9" * 64,
        ),
        _scale_task_payload(
            kind="physical_peft_scale_tier",
            proof_sha256=None,
        ),
        {
            **_scale_task_payload(),
            "scale_plan_sha256": "A" * 64,
        },
        {
            **_scale_task_payload(),
            "scale_tier_id": " bad ",
        },
        {
            **_scale_task_payload(),
            "unexpected": True,
        },
    ),
)
def test_find_pilot_task_rejects_malformed_scale_identity(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload=payload,
    )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="exactly one"):
        driver._find_pilot_task(
            store,
            workspace_id="evaluation-workspace",
            job_id="pilot-job",
        )


def test_find_pilot_task_rejects_ambiguous_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    queue = TaskQueue(store)
    for _ in range(2):
        queue.create(
            workspace_id="evaluation-workspace",
            agent_id="physical-peft-pilot",
            payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
        )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="exactly one"):
        driver._find_pilot_task(
            store,
            workspace_id="evaluation-workspace",
            job_id="pilot-job",
        )


def _trusted_progression_proof() -> driver.TrainingScaleProgressionProof:
    return training_scale._build_progression_proof(
        plan_sha256="1" * 64,
        tier_index=0,
        authorization_sha256="2" * 64,
        job_id="pilot-job",
        job_fingerprint="3" * 64,
        base_artifact_ref="models/base",
        base_sha256="4" * 64,
        candidate_artifact_ref="models/pilot-candidate",
        candidate_sha256="5" * 64,
        frozen_package_sha256="6" * 64,
        training_material_sha256="7" * 64,
        execution_plan_sha256="8" * 64,
        comparison_evidence_sha256="9" * 64,
        evaluation_set_sha256="a" * 64,
    )


def test_scale_progression_record_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "progression.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
    )
    ledger = driver.IdempotencyLedger(store)
    proof = _trusted_progression_proof()

    first = driver._persist_scale_progression_record(
        ledger=ledger,
        task_id=task.task_id,
        proof=proof,
    )
    second = driver._persist_scale_progression_record(
        ledger=ledger,
        task_id=task.task_id,
        proof=proof,
    )

    assert first.status is driver.IdempotencyStatus.COMPLETED
    assert second == first
    assert first.operation_type == driver._SCALE_PROGRESSION_OPERATION_TYPE
    assert first.result == {
        "schema": "nika-physical-scale-progression-record-v1",
        "proof_sha256": proof.proof_sha256,
        "proof": proof.canonical_payload(),
    }


def test_scale_progression_record_rejects_incomplete_existing_state(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "progression.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
    )
    ledger = driver.IdempotencyLedger(store)
    proof = _trusted_progression_proof()
    operation_key, input_fingerprint, _ = driver._scale_progression_record_identity(
        proof
    )
    ledger.reserve_once(
        operation_key=operation_key,
        task_id=task.task_id,
        operation_type=driver._SCALE_PROGRESSION_OPERATION_TYPE,
        input_fingerprint=input_fingerprint,
    )

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="incomplete or inconsistent",
    ):
        driver._persist_scale_progression_record(
            ledger=ledger,
            task_id=task.task_id,
            proof=proof,
        )


def _complete_promoted_experiment(
    *,
    store: SQLiteStore,
    experiment_id: str,
    champion: driver.ModelCandidate,
    challenger: driver.ModelCandidate,
    config: driver.PhysicalEvaluationConfig,
    evaluation: driver.EvaluationSet,
) -> None:
    repository = SQLiteExperimentRepository(store)
    driver._claim_evaluation_attempt(
        repository=repository,
        experiment_id=experiment_id,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
    )
    engine = driver.ExperimentEngine(repository)
    for candidate_id, quality, task_pass in (
        (champion.candidate_id, 0.0, 1.0),
        (challenger.candidate_id, 1.0, 1.0),
    ):
        engine.record(
            experiment_id,
            MetricObservation(
                candidate_id=candidate_id,
                replay_id="case-one",
                metric="model_quality_score",
                value=quality,
            ),
        )
        engine.record(
            experiment_id,
            MetricObservation(
                candidate_id=candidate_id,
                replay_id="case-one",
                metric="model_task_pass",
                value=task_pass,
            ),
        )
    terminal = engine.complete(experiment_id)
    assert terminal.status is ExperimentStatus.PROMOTED


def _durable_loader_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy_report: bool = False,
) -> tuple[Path, dict[str, object], driver.TrainingScaleProgressionProof]:
    root = (tmp_path / "previous-run").resolve()
    root.mkdir()
    database_path = root / "physical-pilot.sqlite3"
    store = SQLiteStore(database_path)
    store.initialize()
    pilot = _pilot_report()
    plan_payload = _scale_plan_payload()
    plan_payload["evaluation_set_sha256"] = "a" * 64
    plan = driver.TrainingScalePlan.from_canonical_payload(plan_payload)
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload={
            "job_id": pilot.job_id,
            "kind": "physical_peft_pilot",
            "progression_proof_sha256": None,
            "scale_plan": plan.canonical_payload(),
            "scale_plan_sha256": plan.plan_sha256,
            "scale_tier_id": "pilot",
        },
    )

    config = _config(tmp_path)
    champion, challenger = _physical_candidates(tmp_path)
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )
    experiment_id = "durable-progression-experiment"
    _complete_promoted_experiment(
        store=store,
        experiment_id=experiment_id,
        champion=champion,
        challenger=challenger,
        config=config,
        evaluation=evaluation,
    )
    snapshot = SQLiteExperimentRepository(store).get(experiment_id)
    definition_sha256, observations_sha256, observation_count = (
        driver.experiment_snapshot_evidence_identity(snapshot)
    )
    comparison_payload = {
        "schema": "nika-attested-training-comparison-v1",
        "experiment_id": experiment_id,
        "experiment_status": snapshot.status.value,
        "selected_candidate_id": snapshot.selected_candidate_id,
        "previous_champion_id": snapshot.previous_champion_id,
        "training_binding_sha256": "b" * 64,
        "champion_binding_sha256": "f" * 64,
        "champion_benchmark_sha256": "c" * 64,
        "challenger_benchmark_sha256": "d" * 64,
        "attestor_id": "evaluator-artifact",
        "attestor_sha256": "e" * 64,
        "definition_sha256": definition_sha256,
        "observations_sha256": observations_sha256,
        "observation_count": observation_count,
    }
    comparison_evidence_sha256 = (
        driver.attested_training_comparison_evidence_sha256(
            comparison_payload
        )
    )
    claim = {
        "authorization_sha256": pilot.scale_authorization_sha256,
        "base_artifact_ref": "models/base",
        "base_sha256": pilot.base_sha256,
        "candidate_artifact_ref": pilot.candidate_artifact_ref,
        "candidate_sha256": pilot.candidate_sha256,
        "comparison_evidence_sha256": comparison_evidence_sha256,
        "evaluation_set_sha256": plan.evaluation_set_sha256,
        "execution_plan_sha256": pilot.execution_plan_sha256,
        "frozen_package_sha256": pilot.frozen_package_sha256,
        "job_fingerprint": pilot.job_fingerprint,
        "job_id": pilot.job_id,
        "plan_sha256": plan.plan_sha256,
        "tier_index": 0,
        "training_material_sha256": pilot.training_material_sha256,
    }
    proof = training_scale._build_progression_proof(
        plan_sha256=claim["plan_sha256"],
        tier_index=claim["tier_index"],
        authorization_sha256=claim["authorization_sha256"],
        job_id=claim["job_id"],
        job_fingerprint=claim["job_fingerprint"],
        base_artifact_ref=claim["base_artifact_ref"],
        base_sha256=claim["base_sha256"],
        candidate_artifact_ref=claim["candidate_artifact_ref"],
        candidate_sha256=claim["candidate_sha256"],
        frozen_package_sha256=claim["frozen_package_sha256"],
        training_material_sha256=claim["training_material_sha256"],
        execution_plan_sha256=claim["execution_plan_sha256"],
        comparison_evidence_sha256=claim["comparison_evidence_sha256"],
        evaluation_set_sha256=claim["evaluation_set_sha256"],
    )
    ledger = driver.IdempotencyLedger(store)
    driver._persist_scale_progression_record(
        ledger=ledger,
        task_id=task.task_id,
        proof=proof,
    )

    evaluation_result = {
        "schema_version": (
            driver._LEGACY_REPORT_SCHEMA_VERSION
            if legacy_report
            else driver._REPORT_SCHEMA_VERSION
        ),
        "schema": driver._LEGACY_REPORT_SCHEMA if legacy_report else driver._REPORT_SCHEMA,
        "physical_pilot_evidence_sha256": pilot.evidence_sha256,
        "requested_experiment_id": config.experiment_id,
        "evaluation_set_sha256": claim["evaluation_set_sha256"],
        "execution_config_sha256": config.benchmark.evidence_sha256,
        "comparison_evidence_sha256": claim["comparison_evidence_sha256"],
        "experiment_id": experiment_id,
        "experiment_status": "promoted",
        "selected_candidate_id": claim["candidate_artifact_ref"],
        "previous_champion_id": claim["base_artifact_ref"],
        "training_binding_sha256": "b" * 64,
        "champion_benchmark_sha256": "c" * 64,
        "challenger_benchmark_sha256": "d" * 64,
        "attestor_id": "evaluator-artifact",
        "attestor_sha256": "e" * 64,
        "champion_provider_manifest_sha256": None,
        "challenger_provider_manifest_sha256": None,
    }
    if not legacy_report:
        evaluation_result.update(
            {
                "champion_binding_sha256": "f" * 64,
                "definition_sha256": definition_sha256,
                "observations_sha256": observations_sha256,
                "observation_count": observation_count,
            }
        )
    eval_record, created = ledger.reserve_once(
        operation_key="physical-old-new-effect:" + "f" * 64,
        task_id=task.task_id,
        operation_type=driver._EVALUATION_OPERATION_TYPE,
        input_fingerprint="sha256:" + "1" * 64,
    )
    assert created is True
    ledger.complete_pending_if_matches(
        operation_key=eval_record.operation_key,
        task_id=eval_record.task_id,
        operation_type=eval_record.operation_type,
        input_fingerprint=eval_record.input_fingerprint,
        created_at=eval_record.created_at,
        result=evaluation_result,
    )

    monkeypatch.setattr(driver, "_physical_report", lambda _: pilot)
    monkeypatch.setattr(
        driver,
        "_verify_completed_checkpoint",
        lambda *_args, **_kwargs: None,
    )
    return root, claim, proof

def test_durable_progression_loader_restores_only_completed_promoted_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, claim, proof = _durable_loader_fixture(tmp_path, monkeypatch)

    restored = driver.load_trusted_scale_progression_proof(
        root,
        workspace_id="evaluation-workspace",
        expected_claim=claim,
    )

    assert restored.canonical_payload() == proof.canonical_payload()
    assert restored.proof_sha256 == proof.proof_sha256


def test_durable_progression_loader_rejects_legacy_evaluation_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, claim, _ = _durable_loader_fixture(
        tmp_path,
        monkeypatch,
        legacy_report=True,
    )

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="canonical evaluation report",
    ):
        driver.load_trusted_scale_progression_proof(
            root,
            workspace_id="evaluation-workspace",
            expected_claim=claim,
        )


def test_durable_progression_loader_rejects_inconsistent_comparison_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, claim, _ = _durable_loader_fixture(tmp_path, monkeypatch)
    store = SQLiteStore(root / "physical-pilot.sqlite3")
    store.initialize()
    task = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )
    ledger = driver.IdempotencyLedger(store)
    record = driver._completed_progression_evaluation_record(
        ledger,
        task_id=task.task_id,
        comparison_evidence_sha256=claim["comparison_evidence_sha256"],
    )
    tampered_result = dict(record.result)
    tampered_result["champion_benchmark_sha256"] = "0" * 64
    tampered = replace(record, result=tampered_result)

    monkeypatch.setattr(
        driver,
        "_completed_progression_evaluation_record",
        lambda *_args, **_kwargs: tampered,
    )

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="comparison evidence digest is inconsistent",
    ):
        driver.load_trusted_scale_progression_proof(
            root,
            workspace_id="evaluation-workspace",
            expected_claim=claim,
        )


@pytest.mark.parametrize("tamper", ("definition", "observations"))
def test_durable_progression_loader_rejects_changed_experiment_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    root, claim, _ = _durable_loader_fixture(tmp_path, monkeypatch)
    original_get = SQLiteExperimentRepository.get

    def changed_get(
        repository: SQLiteExperimentRepository,
        experiment_id: str,
    ) -> object:
        snapshot = original_get(repository, experiment_id)
        if experiment_id != "durable-progression-experiment":
            return snapshot
        if tamper == "definition":
            policy = replace(
                snapshot.definition.policy,
                minimum_improvement=(
                    float(snapshot.definition.policy.minimum_improvement) + 0.125
                ),
            )
            definition = replace(snapshot.definition, policy=policy)
            return replace(snapshot, definition=definition)
        observations = list(snapshot.observations)
        observations[0] = replace(
            observations[0],
            value=float(observations[0].value) + 0.125,
        )
        return replace(snapshot, observations=tuple(observations))

    monkeypatch.setattr(SQLiteExperimentRepository, "get", changed_get)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="experiment evidence changed after evaluation",
    ):
        driver.load_trusted_scale_progression_proof(
            root,
            workspace_id="evaluation-workspace",
            expected_claim=claim,
        )


def test_higher_tier_evaluation_reuses_durable_predecessor_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = driver.TrainingScalePlan.from_canonical_payload(_scale_plan_payload())
    prior = training_scale._build_progression_proof(
        plan_sha256=plan.plan_sha256,
        tier_index=0,
        authorization_sha256="2" * 64,
        job_id="pilot-job",
        job_fingerprint="3" * 64,
        base_artifact_ref="models/base",
        base_sha256="4" * 64,
        candidate_artifact_ref="models/pilot-candidate",
        candidate_sha256="5" * 64,
        frozen_package_sha256="6" * 64,
        training_material_sha256="7" * 64,
        execution_plan_sha256="8" * 64,
        comparison_evidence_sha256="9" * 64,
        evaluation_set_sha256=plan.evaluation_set_sha256,
    )
    pilot = _pilot_report()
    task = SimpleNamespace(
        payload={
            **_scale_task_payload_with_chain(
                kind="physical_peft_scale_tier",
                proof=prior,
            ),
            "job_id": pilot.job_id,
            "scale_tier_id": "small",
        }
    )
    material = SimpleNamespace(
        training_material_sha256=pilot.training_material_sha256,
    )
    observed: dict[str, object] = {}
    authorization = SimpleNamespace(
        authorization_sha256=pilot.scale_authorization_sha256,
    )

    monkeypatch.setattr(
        driver,
        "reconstruct_training_material_evidence",
        lambda *_args, **_kwargs: material,
    )

    def authorize(**kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return authorization

    monkeypatch.setattr(driver, "authorize_training_scale", authorize)
    run = SimpleNamespace(base_artifact=object())

    context = driver._reconstruct_scale_progression_context(
        task=task,
        package=object(),
        workspace_id="evaluation-workspace",
        pilot=pilot,
        run=run,
    )

    assert context is not None
    assert context.plan.plan_sha256 == plan.plan_sha256
    assert observed["progression_proof"] == prior
    assert observed["tier_id"] == "small"


def test_durable_progression_loader_rejects_forged_claim_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, claim, _ = _durable_loader_fixture(tmp_path, monkeypatch)
    forged = dict(claim)
    forged["comparison_evidence_sha256"] = "8" * 64

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="completed durable scale progression record",
    ):
        driver.load_trusted_scale_progression_proof(
            root,
            workspace_id="evaluation-workspace",
            expected_claim=forged,
        )


def test_candidate_descriptor_must_match_physical_pilot_digest() -> None:
    descriptor = driver._candidate_descriptor(
        _candidate_config(),
        report=_pilot_report(),
    )

    assert descriptor.descriptor_digest == _candidate_descriptor().descriptor_digest
    assert descriptor.registry_key == _candidate_descriptor().registry_key


def test_candidate_descriptor_metadata_substitution_is_rejected() -> None:
    substituted = driver.CandidateModelConfig(
        model_id="other-adapter",
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
    )

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="does not match physical pilot evidence",
    ):
        driver._candidate_descriptor(substituted, report=_pilot_report())


def test_windows_pe_header_admission_accepts_canonical_signature(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "evaluator.exe").resolve()
    payload = bytearray(68)
    payload[:2] = b"MZ"
    payload[60:64] = (64).to_bytes(4, "little")
    payload[64:68] = b"PE\0\0"
    executable.write_bytes(payload)

    driver._require_windows_pe_executable(executable, name="evaluator executable")


def test_windows_pe_header_admission_rejects_renamed_non_pe(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "evaluator.exe").resolve()
    executable.write_bytes(b"not-a-windows-program")

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="not a valid Windows PE executable",
    ):
        driver._require_windows_pe_executable(executable, name="evaluator executable")


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_regular_authority_reader_refuses_writer_and_releases_share_fence(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "authority.json").resolve()
    path.write_bytes(b'{"trusted":true}')

    with path.open("r+b"):
        with pytest.raises(
            driver.PhysicalEvaluationDriverError,
            match="authority input could not be read",
        ):
            driver._read_regular_file(
                path,
                name="authority input",
                max_bytes=1024,
            )

    assert driver._read_regular_file(
        path,
        name="authority input",
        max_bytes=1024,
    ) == b'{"trusted":true}'
    path.write_bytes(b'{"replacement":true}')
    assert path.read_bytes() == b'{"replacement":true}'


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_windows_pe_header_reader_refuses_preexisting_writer(tmp_path: Path) -> None:
    executable = (tmp_path / "evaluator.exe").resolve()
    payload = bytearray(68)
    payload[:2] = b"MZ"
    payload[60:64] = (64).to_bytes(4, "little")
    payload[64:68] = b"PE\0\0"
    executable.write_bytes(payload)

    with executable.open("r+b"):
        with pytest.raises(
            driver.PhysicalEvaluationDriverError,
            match="Windows PE header could not be read",
        ):
            driver._require_windows_pe_executable(
                executable,
                name="evaluator executable",
            )


def test_model_size_preflight_does_not_read_model_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = (tmp_path / "large-model.gguf").resolve()
    model.write_bytes(b"x" * 8192)

    def fail_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("model preflight must not open/read the entire artifact")

    monkeypatch.setattr(Path, "open", fail_open)

    assert driver._model_size(model, name="model") == 8192


def _physical_candidates(
    tmp_path: Path,
) -> tuple[driver.ModelCandidate, driver.ModelCandidate]:
    config = _config(tmp_path)
    base_descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="local-base",
        model_id="base-model",
        model_version="v1",
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256="1" * 64,
        size_bytes=456,
        capabilities=("text",),
    )
    challenger_descriptor = _candidate_descriptor()
    return (
        driver._candidate(
            candidate_id="models/base",
            descriptor=base_descriptor,
            evaluator=config.evaluator,
        ),
        driver._candidate(
            candidate_id="models/pilot-candidate",
            descriptor=challenger_descriptor,
            evaluator=config.evaluator,
        ),
    )


def test_durable_attempt_claim_precedes_and_fences_repeat_effects(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    champion, challenger = _physical_candidates(tmp_path)
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )
    store = SQLiteStore(tmp_path / "attempt.sqlite3")
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    _, _, attempt_id = driver._evaluation_effect_identity(
        requested_experiment_id=config.experiment_id,
        pilot=_pilot_report(),
        training_binding_sha256="a" * 64,
        champion_binding_sha256="b" * 64,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
        attestor_id="evaluator-artifact",
        attestor_sha256="c" * 64,
    )

    driver._claim_evaluation_attempt(
        repository=repository,
        experiment_id=attempt_id,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
    )

    snapshot = repository.get(attempt_id)
    assert snapshot.status is ExperimentStatus.RUNNING
    assert snapshot.observations == ()

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="will not repeat champion/challenger effects",
    ):
        driver._claim_evaluation_attempt(
            repository=repository,
            experiment_id=attempt_id,
            champion=champion,
            challenger=challenger,
            evaluation_set=evaluation,
            execution_config=config.benchmark,
            policy=config.policy,
            permission_fingerprint=config.permission_fingerprint,
        )


def test_physical_attempt_identity_binds_exact_attestor(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    champion, challenger = _physical_candidates(tmp_path)
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )
    common = {
        "requested_experiment_id": config.experiment_id,
        "pilot": _pilot_report(),
        "training_binding_sha256": "a" * 64,
        "champion_binding_sha256": "b" * 64,
        "champion": champion,
        "challenger": challenger,
        "evaluation_set": evaluation,
        "execution_config": config.benchmark,
        "policy": config.policy,
        "permission_fingerprint": config.permission_fingerprint,
        "attestor_id": "evaluator-artifact",
    }

    first_operation, first_input, first_experiment = driver._evaluation_effect_identity(
        **common,
        attestor_sha256="c" * 64,
    )
    second_operation, second_input, second_experiment = driver._evaluation_effect_identity(
        **common,
        attestor_sha256="d" * 64,
    )

    assert first_operation != second_operation
    assert first_input != second_input
    assert first_experiment != second_experiment
    assert first_operation.startswith("physical-old-new-effect:")
    assert first_experiment.startswith("nika-physical-old-new-")


def test_requested_experiment_label_cannot_bypass_effect_fence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    champion, challenger = _physical_candidates(tmp_path)
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )
    common = {
        "pilot": _pilot_report(),
        "training_binding_sha256": "a" * 64,
        "champion_binding_sha256": "b" * 64,
        "champion": champion,
        "challenger": challenger,
        "evaluation_set": evaluation,
        "execution_config": config.benchmark,
        "policy": config.policy,
        "permission_fingerprint": config.permission_fingerprint,
        "attestor_id": "evaluator-artifact",
        "attestor_sha256": "c" * 64,
    }

    first_operation, first_input, _ = driver._evaluation_effect_identity(
        requested_experiment_id="label-one",
        **common,
    )
    second_operation, second_input, _ = driver._evaluation_effect_identity(
        requested_experiment_id="label-two",
        **common,
    )

    assert first_operation == second_operation
    assert first_input != second_input


def test_idempotency_reservation_blocks_same_effect_with_changed_input(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "effect-ledger.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
    )
    ledger = driver.IdempotencyLedger(store)

    first, created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key="physical-old-new-effect:" + "a" * 64,
        input_fingerprint="sha256:" + "b" * 64,
    )
    replay, replay_created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key=first.operation_key,
        input_fingerprint=first.input_fingerprint,
    )

    assert created is True
    assert replay_created is False
    assert replay == first
    assert replay.status is driver.IdempotencyStatus.PENDING

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="already bound to different",
    ):
        driver._reserve_evaluation_effect(
            ledger=ledger,
            task_id=task.task_id,
            operation_key=first.operation_key,
            input_fingerprint="sha256:" + "c" * 64,
        )


def test_interrupted_effect_reservation_becomes_uncertain(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "uncertain-ledger.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
    )
    ledger = driver.IdempotencyLedger(store)
    reservation, created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key="physical-old-new-effect:" + "a" * 64,
        input_fingerprint="sha256:" + "b" * 64,
    )
    assert created is True

    driver._mark_evaluation_uncertain(ledger, reservation)

    persisted = ledger.require(reservation.operation_key)
    assert persisted.status is driver.IdempotencyStatus.UNCERTAIN
    replay, replay_created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key=reservation.operation_key,
        input_fingerprint=reservation.input_fingerprint,
    )
    assert replay_created is False
    assert replay.status is driver.IdempotencyStatus.UNCERTAIN

def test_completed_ledger_result_recovers_without_new_effect_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    champion, challenger = _physical_candidates(tmp_path)
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )
    store = SQLiteStore(tmp_path / "recovery.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
    )
    ledger = driver.IdempotencyLedger(store)
    repository = SQLiteExperimentRepository(store)
    operation_key, input_fingerprint, experiment_id = (
        driver._evaluation_effect_identity(
            requested_experiment_id=config.experiment_id,
            pilot=_pilot_report(),
            training_binding_sha256="a" * 64,
            champion_binding_sha256="b" * 64,
            champion=champion,
            challenger=challenger,
            evaluation_set=evaluation,
            execution_config=config.benchmark,
            policy=config.policy,
            permission_fingerprint=config.permission_fingerprint,
            attestor_id="evaluator-artifact",
            attestor_sha256="c" * 64,
        )
    )
    reservation, created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key=operation_key,
        input_fingerprint=input_fingerprint,
    )
    assert created is True

    driver._claim_evaluation_attempt(
        repository=repository,
        experiment_id=experiment_id,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
    )
    engine = driver.ExperimentEngine(repository)
    for candidate_id, quality, task_pass in (
        (champion.candidate_id, 0.0, 1.0),
        (challenger.candidate_id, 1.0, 1.0),
    ):
        engine.record(
            experiment_id,
            MetricObservation(
                candidate_id=candidate_id,
                replay_id="case-one",
                metric="model_quality_score",
                value=quality,
            ),
        )
        engine.record(
            experiment_id,
            MetricObservation(
                candidate_id=candidate_id,
                replay_id="case-one",
                metric="model_task_pass",
                value=task_pass,
            ),
        )
    terminal = engine.complete(experiment_id)
    assert terminal.status is ExperimentStatus.PROMOTED

    payload = {
        "schema_version": 1,
        "schema": "nika-physical-old-new-evaluation-report-v1",
        "physical_pilot_evidence_sha256": _pilot_report().evidence_sha256,
        "requested_experiment_id": config.experiment_id,
        "evaluation_set_sha256": evaluation.content_sha256,
        "execution_config_sha256": config.benchmark.evidence_sha256,
        "comparison_evidence_sha256": "d" * 64,
        "experiment_id": experiment_id,
        "experiment_status": terminal.status.value,
        "selected_candidate_id": terminal.selected_candidate_id,
        "previous_champion_id": terminal.previous_champion_id,
        "training_binding_sha256": "a" * 64,
        "champion_benchmark_sha256": "e" * 64,
        "challenger_benchmark_sha256": "f" * 64,
        "attestor_id": "evaluator-artifact",
        "attestor_sha256": "c" * 64,
        "champion_provider_manifest_sha256": None,
        "challenger_provider_manifest_sha256": None,
    }
    ledger.complete_pending_if_matches(
        operation_key=reservation.operation_key,
        task_id=reservation.task_id,
        operation_type=reservation.operation_type,
        input_fingerprint=reservation.input_fingerprint,
        created_at=reservation.created_at,
        result=payload,
    )

    replay, replay_created = driver._reserve_evaluation_effect(
        ledger=ledger,
        task_id=task.task_id,
        operation_key=operation_key,
        input_fingerprint=input_fingerprint,
    )
    assert replay_created is False
    assert replay.status is driver.IdempotencyStatus.COMPLETED
    recovered = driver._validate_recovered_report_payload(
        replay.result,
        pilot=_pilot_report(),
        requested_experiment_id=config.experiment_id,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        experiment_id=experiment_id,
        training_binding_sha256="a" * 64,
        attestor_id="evaluator-artifact",
        attestor_sha256="c" * 64,
    )
    driver._validate_recovered_experiment(
        repository=repository,
        experiment_id=experiment_id,
        champion=champion,
        challenger=challenger,
        evaluation_set=evaluation,
        execution_config=config.benchmark,
        policy=config.policy,
        permission_fingerprint=config.permission_fingerprint,
        report_payload=recovered,
    )
    assert recovered == payload

    boolean_version = dict(payload)
    boolean_version["schema_version"] = True
    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="invalid report schema version",
    ):
        driver._validate_recovered_report_payload(
            boolean_version,
            pilot=_pilot_report(),
            requested_experiment_id=config.experiment_id,
            evaluation_set=evaluation,
            execution_config=config.benchmark,
            experiment_id=experiment_id,
            training_binding_sha256="a" * 64,
            attestor_id="evaluator-artifact",
            attestor_sha256="c" * 64,
        )


def test_report_writer_is_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"
    path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="already exists",
    ):
        driver._write_report(path, {"schema": "new"})

    assert path.read_text(encoding="utf-8") == "existing\n"


def test_report_writer_cleans_temporary_after_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"
    monkeypatch.setattr(driver.os, "name", "posix")

    def fail_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert source.parent == tmp_path
        assert destination == path
        assert follow_symlinks is False
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(driver.os, "link", fail_link)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="could not be persisted",
    ):
        driver._write_report(path, {"schema": "test"})

    assert not path.exists()
    assert not tuple(tmp_path.glob(".physical-evaluation-report.*.tmp"))


def test_evaluation_set_allows_multiline_payload_text() -> None:
    payload = _evaluation_payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    messages = case["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    message["content"] = "line one\nline two"
    case["expected_text"] = "answer line one\nanswer line two"

    evaluation = driver._evaluation_set_from_json(
        json.dumps(payload, ensure_ascii=False)
    )

    assert evaluation.cases[0].messages[0].content == "line one\nline two"
    assert evaluation.cases[0].expected_text == "answer line one\nanswer line two"


def test_numeric_overflow_is_reported_as_driver_error(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    benchmark = payload["benchmark"]
    assert isinstance(benchmark, dict)
    benchmark["timeout_seconds"] = 10**10000

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="must be a finite number",
    ):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))
