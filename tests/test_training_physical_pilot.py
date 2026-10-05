from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import nika_core.training_physical_pilot as pilot
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.training_physical_pilot import (
    PhysicalTrainingPilotError,
    PhysicalTrainingPilotReport,
    build_physical_training_pilot_report,
    run_physical_training_pilot,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
)


@pytest.fixture(autouse=True)
def _windows_report_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nika_core.training_physical_pilot._is_windows",
        lambda: True,
    )
    monkeypatch.setattr(
        "nika_core.training_physical_pilot.candidate_adapter_manifest",
        lambda _: _candidate_manifest(),
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate_manifest(
    *,
    job_fingerprint: str = "f" * 64,
    previous_adapter_tensors_sha256: str = "3" * 64,
    trained_adapter_tensors_sha256: str = "4" * 64,
) -> dict[str, object]:
    return {
        "base_artifact_ref": "models/base",
        "base_artifact_sha256": "a" * 64,
        "candidate_artifact_ref": "models/candidate/pilot",
        "consumed_materials_sha256": "1" * 64,
        "job_fingerprint": job_fingerprint,
        "model_dir_manifest_sha256": "2" * 64,
        "previous_adapter_tensors_sha256": previous_adapter_tensors_sha256,
        "schema": "nika-peft-candidate-v2",
        "step_number": 2,
        "trained_adapter_tensors_sha256": trained_adapter_tensors_sha256,
        "trainer_artifact_id": "5" * 64,
        "trainer_implementation_sha256": "6" * 64,
        "trainer_sha256": "7" * 64,
        "training_runtime_manifest_sha256": "8" * 64,
    }


def _descriptor(path: Path, *, payload: bytes | None = None) -> ModelArtifactDescriptor:
    body = path.read_bytes() if payload is None else payload
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="training-runtime",
        model_id="physical-pilot-candidate",
        model_version="pilot-1",
        source_reference="https://models.example.test/nika/physical-pilot",
        license_reference="https://licenses.example.test/nika/physical-pilot",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_sha256(body),
        size_bytes=len(body),
        capabilities=("text",),
    )



def _job_spec() -> TrainingJobSpec:
    return TrainingJobSpec(
        job_id="pilot-job",
        task_id="pilot-task",
        project_id="pilot-project",
        owner_id="pilot-owner",
        base_artifact=ArtifactIdentity("models/base", "a" * 64),
        frozen_package_sha256="b" * 64,
        training_material_sha256="c" * 64,
        scale_authorization_sha256="d" * 64,
        candidate_artifact_ref="models/candidate/pilot",
        max_steps=2,
    )


def _run_evidence(
    *,
    state: TrainingRunState,
    next_step: int,
    checkpoint_id: str,
    candidate_sha256: str | None = None,
    job_fingerprint: str | None = None,
    reason: str | None = None,
) -> TrainingRunEvidence:
    effective_reason = reason
    if effective_reason is None and state is TrainingRunState.PAUSED:
        effective_reason = "paused"
    return TrainingRunEvidence(
        job_id="pilot-job",
        state=state,
        next_step=next_step,
        base_artifact=ArtifactIdentity("models/base", "a" * 64),
        frozen_package_sha256="b" * 64,
        training_material_sha256="c" * 64,
        scale_authorization_sha256="d" * 64,
        execution_plan_sha256="e" * 64,
        job_fingerprint=job_fingerprint or "f" * 64,
        candidate_artifact_ref="models/candidate/pilot",
        candidate_sha256=candidate_sha256,
        checkpoint_id=checkpoint_id,
        reason=effective_reason,
    )


def _completed_for(payload: bytes) -> TrainingRunEvidence:
    return _run_evidence(
        state=TrainingRunState.COMPLETED,
        next_step=2,
        checkpoint_id="checkpoint-completed",
        candidate_sha256=_sha256(payload),
    )


def _restart_probe(
    *,
    next_step: int = 1,
    checkpoint_id: str = "checkpoint-restart",
    reason: str = "paused_before_admission",
) -> TrainingRunEvidence:
    return _run_evidence(
        state=TrainingRunState.PAUSED,
        next_step=next_step,
        checkpoint_id=checkpoint_id,
        reason=reason,
    )


def _build_report(tmp_path: Path, payload: bytes = b"candidate") -> PhysicalTrainingPilotReport:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    return build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
        restart_probe=_restart_probe(),
        completed=_completed_for(payload),
        candidate_path=candidate,
        candidate_descriptor=_descriptor(candidate),
        candidate_root=tmp_path,
    )



def test_job_spec_snapshot_rejects_boolean_step_carrier() -> None:
    spec = _job_spec()
    object.__setattr__(spec, "max_steps", True)

    with pytest.raises(PhysicalTrainingPilotError, match="not canonical"):
        pilot._snapshot_job_spec(spec)


def test_descriptor_factory_runs_after_completed_evidence_and_receives_detached_copy(
    tmp_path: Path,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    completed = _completed_for(payload)
    observed: list[TrainingRunEvidence] = []

    def factory(evidence: TrainingRunEvidence) -> ModelArtifactDescriptor:
        observed.append(evidence)
        object.__setattr__(evidence, "job_id", "mutated-callback-copy")
        return _descriptor(candidate)

    descriptor = pilot._resolve_candidate_descriptor(factory, completed)

    assert type(descriptor) is ModelArtifactDescriptor
    assert len(observed) == 1
    assert observed[0] is not completed
    assert completed.job_id == "pilot-job"


def test_descriptor_factory_requires_canonical_descriptor() -> None:
    completed = _completed_for(b"candidate")

    with pytest.raises(TypeError, match="exact ModelArtifactDescriptor"):
        pilot._resolve_candidate_descriptor(
            lambda _: object(),  # type: ignore[return-value]
            completed,
        )


def test_build_report_binds_restart_and_canonical_candidate_receipt(
    tmp_path: Path,
) -> None:
    payload = b"physical-pilot-candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    descriptor = _descriptor(candidate)

    report = build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
        restart_probe=_restart_probe(),
        completed=_completed_for(payload),
        candidate_path=candidate,
        candidate_descriptor=descriptor,
        candidate_root=tmp_path,
    )

    assert report.platform == "windows"
    assert report.schema_version == 3
    assert report.completed_steps == 2
    assert report.consumed_materials_sha256 == "1" * 64
    assert report.model_dir_manifest_sha256 == "2" * 64
    assert report.previous_adapter_tensors_sha256 == "3" * 64
    assert report.trained_adapter_tensors_sha256 == "4" * 64
    assert report.trainer_artifact_id == "5" * 64
    assert report.trainer_implementation_sha256 == "6" * 64
    assert report.trainer_sha256 == "7" * 64
    assert report.training_runtime_manifest_sha256 == "8" * 64
    assert report.candidate_sha256 == _sha256(payload)
    assert report.candidate_byte_count == len(payload)
    assert report.candidate_descriptor_sha256 == descriptor.descriptor_digest
    assert report.candidate_registry_key == descriptor.registry_key
    assert report.paused_checkpoint_id == "checkpoint-paused"
    assert report.restart_checkpoint_id == "checkpoint-restart"
    assert report.completed_checkpoint_id == "checkpoint-completed"


def test_build_report_rejects_manifest_without_weight_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    monkeypatch.setattr(
        pilot,
        "candidate_adapter_manifest",
        lambda _: _candidate_manifest(
            trained_adapter_tensors_sha256="3" * 64,
        ),
    )

    with pytest.raises(PhysicalTrainingPilotError, match="weight mutation"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_rejects_manifest_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    monkeypatch.setattr(
        pilot,
        "candidate_adapter_manifest",
        lambda _: _candidate_manifest(job_fingerprint="0" * 64),
    )

    with pytest.raises(PhysicalTrainingPilotError, match="runtime identity"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_report_round_trip_is_canonical_and_digest_stable(tmp_path: Path) -> None:
    report = _build_report(tmp_path)

    restored = PhysicalTrainingPilotReport.from_json(report.to_json())

    assert restored == report
    assert restored.evidence_sha256 == report.evidence_sha256
    assert report.to_json() == json.dumps(
        report.canonical_payload(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_report_rejects_unknown_or_duplicate_json_fields(tmp_path: Path) -> None:
    report = _build_report(tmp_path)
    value = report.canonical_payload()
    value["unexpected"] = True

    with pytest.raises(PhysicalTrainingPilotError, match="strict schema"):
        PhysicalTrainingPilotReport.from_json(json.dumps(value))

    duplicate = report.to_json().replace(
        '"job_id":"pilot-job"',
        '"job_id":"pilot-job","job_id":"other"',
    )
    with pytest.raises(PhysicalTrainingPilotError, match="invalid"):
        PhysicalTrainingPilotReport.from_json(duplicate)


def test_build_report_rejects_runtime_candidate_digest_mismatch(tmp_path: Path) -> None:
    payload = b"actual-candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    with pytest.raises(PhysicalTrainingPilotError, match="runtime evidence"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_completed_for(b"different-candidate"),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_rejects_descriptor_digest_mismatch(tmp_path: Path) -> None:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(b"actual-candidate")
    descriptor = _descriptor(candidate, payload=b"different-candidate")

    with pytest.raises(PhysicalTrainingPilotError, match="verification failed"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_completed_for(b"actual-candidate"),
            candidate_path=candidate,
            candidate_descriptor=descriptor,
            candidate_root=tmp_path,
        )


def test_build_report_rejects_restart_probe_without_durable_reopen(
    tmp_path: Path,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    with pytest.raises(PhysicalTrainingPilotError, match="reopen"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(next_step=0),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_requires_effect_free_restart_probe_reason(
    tmp_path: Path,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    with pytest.raises(PhysicalTrainingPilotError, match="before admission"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(reason="paused"),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_rejects_restart_identity_drift(tmp_path: Path) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    with pytest.raises(PhysicalTrainingPilotError, match="job_fingerprint"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_run_evidence(
                state=TrainingRunState.COMPLETED,
                next_step=2,
                checkpoint_id="checkpoint-completed",
                candidate_sha256=_sha256(payload),
                job_fingerprint="1" * 64,
            ),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_rejects_boolean_step_carrier(tmp_path: Path) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    paused = _run_evidence(
        state=TrainingRunState.PAUSED,
        next_step=1,
        checkpoint_id="checkpoint-paused",
    )
    object.__setattr__(paused, "next_step", True)

    with pytest.raises(PhysicalTrainingPilotError, match="step boundary"):
        build_physical_training_pilot_report(
            paused=paused,
            restart_probe=_restart_probe(),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_rejects_distinct_checkpoint_bypass(tmp_path: Path) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    with pytest.raises(PhysicalTrainingPilotError, match="checkpoint"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="same-checkpoint",
            ),
            restart_probe=_restart_probe(),
            completed=_run_evidence(
                state=TrainingRunState.COMPLETED,
                next_step=2,
                checkpoint_id="same-checkpoint",
                candidate_sha256=_sha256(payload),
            ),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )



def test_build_report_rejects_non_windows_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(
        "nika_core.training_physical_pilot._is_windows",
        lambda: False,
    )

    with pytest.raises(PhysicalTrainingPilotError, match="built on Windows"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            restart_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_build_report_requires_explicit_pause_reason(tmp_path: Path) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    paused = _run_evidence(
        state=TrainingRunState.PAUSED,
        next_step=1,
        checkpoint_id="checkpoint-paused",
    )
    object.__setattr__(paused, "reason", "resource_revalidation:denied")

    with pytest.raises(PhysicalTrainingPilotError, match="explicit pause control"):
        build_physical_training_pilot_report(
            paused=paused,
            restart_probe=_restart_probe(),
            completed=_completed_for(payload),
            candidate_path=candidate,
            candidate_descriptor=_descriptor(candidate),
            candidate_root=tmp_path,
        )


def test_report_rejects_non_windows_platform(tmp_path: Path) -> None:
    report = _build_report(tmp_path)

    with pytest.raises(PhysicalTrainingPilotError, match="Windows"):
        PhysicalTrainingPilotReport(
            job_id=report.job_id,
            base_sha256=report.base_sha256,
            frozen_package_sha256=report.frozen_package_sha256,
            training_material_sha256=report.training_material_sha256,
            scale_authorization_sha256=report.scale_authorization_sha256,
            execution_plan_sha256=report.execution_plan_sha256,
            job_fingerprint=report.job_fingerprint,
            consumed_materials_sha256=report.consumed_materials_sha256,
            model_dir_manifest_sha256=report.model_dir_manifest_sha256,
            previous_adapter_tensors_sha256=report.previous_adapter_tensors_sha256,
            trained_adapter_tensors_sha256=report.trained_adapter_tensors_sha256,
            trainer_artifact_id=report.trainer_artifact_id,
            trainer_implementation_sha256=report.trainer_implementation_sha256,
            trainer_sha256=report.trainer_sha256,
            training_runtime_manifest_sha256=report.training_runtime_manifest_sha256,
            paused_checkpoint_id=report.paused_checkpoint_id,
            restart_checkpoint_id=report.restart_checkpoint_id,
            completed_checkpoint_id=report.completed_checkpoint_id,
            candidate_artifact_ref=report.candidate_artifact_ref,
            candidate_descriptor_sha256=report.candidate_descriptor_sha256,
            candidate_registry_key=report.candidate_registry_key,
            candidate_sha256=report.candidate_sha256,
            candidate_byte_count=report.candidate_byte_count,
            completed_steps=report.completed_steps,
            platform="linux",
        )


def _install_runner_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resumed_probe: TrainingRunEvidence,
    completed: TrainingRunEvidence,
    initial_pause: TrainingRunEvidence | None = None,
) -> tuple[object, object, list[tuple[str, bool]]]:
    calls: list[tuple[str, bool]] = []

    class FakeRuntime:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        def run(self, *_: object, control: object = None, **__: object) -> TrainingRunEvidence:
            self.calls += 1
            calls.append((self.name, control is not None))
            if self.name == "initial":
                if initial_pause is not None:
                    return initial_pause
                return _run_evidence(
                    state=TrainingRunState.PAUSED,
                    next_step=1,
                    checkpoint_id="checkpoint-paused",
                )
            if self.calls == 1:
                return resumed_probe
            return completed

    class FakeWorker:
        @property
        def execution_plan_sha256(self) -> str:
            return "e" * 64

    class FakeSpec:
        max_steps = 2
        base_artifact = ArtifactIdentity("models/base", "a" * 64)
        job_id = "pilot-job"
        task_id = "pilot-task"
        project_id = "pilot-project"
        owner_id = "pilot-owner"
        frozen_package_sha256 = "b" * 64
        training_material_sha256 = "c" * 64
        scale_authorization_sha256 = "d" * 64
        candidate_artifact_ref = "models/candidate/pilot"
        resource_scope = "model_training"

    class FakeAuthorization:
        pass

    initial_runtime = FakeRuntime("initial")
    resumed_runtime = FakeRuntime("resumed")
    initial_worker = FakeWorker()
    resumed_worker = FakeWorker()
    sentinel = object()

    monkeypatch.setattr(pilot, "TrainingRuntime", FakeRuntime)
    monkeypatch.setattr(pilot, "SubprocessTrainingWorker", FakeWorker)
    monkeypatch.setattr(pilot, "TrainingJobSpec", FakeSpec)
    monkeypatch.setattr(pilot, "TrainingScaleAuthorization", FakeAuthorization)
    monkeypatch.setattr(
        pilot,
        "_snapshot_job_spec",
        lambda _: FakeSpec(),
    )
    monkeypatch.setattr(
        pilot,
        "_resolve_candidate_descriptor",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        pilot,
        "build_physical_training_pilot_report",
        lambda **_: sentinel,
    )

    result = pilot.run_physical_training_pilot(
        runtime=initial_runtime,
        restart_runtime=lambda: resumed_runtime,
        spec=FakeSpec(),
        worker=initial_worker,
        restart_worker=lambda: resumed_worker,
        scale_authorization=FakeAuthorization(),
        candidate_path=Path("candidate"),
        candidate_descriptor_factory=lambda _: object(),  # type: ignore[return-value]
    )
    return result, sentinel, calls


def test_physical_runner_probes_reopened_checkpoint_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, sentinel, calls = _install_runner_fakes(
        monkeypatch,
        resumed_probe=_restart_probe(),
        completed=_completed_for(b"candidate"),
    )

    assert result is sentinel
    assert calls == [
        ("initial", True),
        ("resumed", True),
        ("resumed", False),
    ]


def test_physical_runner_rejects_non_control_initial_pause_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_pause = _run_evidence(
        state=TrainingRunState.PAUSED,
        next_step=1,
        checkpoint_id="checkpoint-paused",
        reason="resource_revalidation:denied",
    )

    with pytest.raises(PhysicalTrainingPilotError, match="explicit pause control"):
        _install_runner_fakes(
            monkeypatch,
            initial_pause=initial_pause,
            resumed_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
        )


def test_physical_runner_rejects_empty_restart_store_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PhysicalTrainingPilotError, match="reopen"):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(next_step=0),
            completed=_completed_for(b"candidate"),
        )


def test_physical_runner_rejects_non_effect_free_restart_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PhysicalTrainingPilotError, match="before admission"):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(reason="paused"),
            completed=_completed_for(b"candidate"),
        )


def test_physical_runner_refuses_non_windows_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nika_core.training_physical_pilot._is_windows",
        lambda: False,
    )

    with pytest.raises(PhysicalTrainingPilotError, match="must execute on Windows"):
        run_physical_training_pilot(
            runtime=object(),  # type: ignore[arg-type]
            restart_runtime=lambda: object(),  # type: ignore[return-value]
            spec=object(),  # type: ignore[arg-type]
            worker=object(),  # type: ignore[arg-type]
            restart_worker=lambda: object(),  # type: ignore[return-value]
            scale_authorization=object(),  # type: ignore[arg-type]
            candidate_path=Path("candidate"),
            candidate_descriptor_factory=lambda _: object(),  # type: ignore[return-value]
        )
