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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def _build_report(tmp_path: Path, payload: bytes = b"candidate") -> PhysicalTrainingPilotReport:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    return build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
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
        completed=_completed_for(payload),
        candidate_path=candidate,
        candidate_descriptor=descriptor,
        candidate_root=tmp_path,
    )

    assert report.platform == "windows"
    assert report.completed_steps == 2
    assert report.candidate_sha256 == _sha256(payload)
    assert report.candidate_byte_count == len(payload)
    assert report.candidate_descriptor_sha256 == descriptor.descriptor_digest
    assert report.candidate_registry_key == descriptor.registry_key
    assert report.paused_checkpoint_id == "checkpoint-paused"
    assert report.completed_checkpoint_id == "checkpoint-completed"


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
            completed=_completed_for(b"actual-candidate"),
            candidate_path=candidate,
            candidate_descriptor=descriptor,
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
            paused_checkpoint_id=report.paused_checkpoint_id,
            completed_checkpoint_id=report.completed_checkpoint_id,
            candidate_artifact_ref=report.candidate_artifact_ref,
            candidate_descriptor_sha256=report.candidate_descriptor_sha256,
            candidate_registry_key=report.candidate_registry_key,
            candidate_sha256=report.candidate_sha256,
            candidate_byte_count=report.candidate_byte_count,
            completed_steps=report.completed_steps,
            platform="linux",
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
            candidate_descriptor=object(),  # type: ignore[arg-type]
        )
