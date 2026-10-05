from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.training_physical_pilot import (
    PhysicalTrainingPilotError,
    PhysicalTrainingPilotReport,
    build_physical_training_pilot_report,
    run_physical_training_pilot,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingRunEvidence,
    TrainingRunState,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_evidence(
    *,
    state: TrainingRunState,
    next_step: int,
    checkpoint_id: str,
    candidate_sha256: str | None = None,
    job_fingerprint: str | None = None,
) -> TrainingRunEvidence:
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
    )


def _completed_for(payload: bytes) -> TrainingRunEvidence:
    return _run_evidence(
        state=TrainingRunState.COMPLETED,
        next_step=2,
        checkpoint_id="checkpoint-completed",
        candidate_sha256=_sha256(payload),
    )


def test_build_report_binds_restart_and_exact_candidate_bytes(tmp_path: Path) -> None:
    payload = b"physical-pilot-candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    report = build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
        completed=_completed_for(payload),
        candidate_path=candidate,
    )

    assert report.platform == "windows"
    assert report.completed_steps == 2
    assert report.candidate_sha256 == _sha256(payload)
    assert report.candidate_byte_count == len(payload)
    assert report.paused_checkpoint_id == "checkpoint-paused"
    assert report.completed_checkpoint_id == "checkpoint-completed"


def test_report_round_trip_is_canonical_and_digest_stable(tmp_path: Path) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    report = build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
        completed=_completed_for(payload),
        candidate_path=candidate,
    )

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
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    report = build_physical_training_pilot_report(
        paused=_run_evidence(
            state=TrainingRunState.PAUSED,
            next_step=1,
            checkpoint_id="checkpoint-paused",
        ),
        completed=_completed_for(payload),
        candidate_path=candidate,
    )
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


def test_build_report_rejects_candidate_digest_mismatch(tmp_path: Path) -> None:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(b"actual-candidate")

    with pytest.raises(PhysicalTrainingPilotError, match="candidate bytes"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            completed=_completed_for(b"different-candidate"),
            candidate_path=candidate,
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
        )


def test_build_report_requires_distinct_durable_checkpoints(tmp_path: Path) -> None:
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
        )


def test_build_report_rejects_empty_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(b"")

    with pytest.raises(PhysicalTrainingPilotError, match="must not be empty"):
        build_physical_training_pilot_report(
            paused=_run_evidence(
                state=TrainingRunState.PAUSED,
                next_step=1,
                checkpoint_id="checkpoint-paused",
            ),
            completed=_completed_for(b"candidate"),
            candidate_path=candidate,
        )


def test_report_rejects_non_windows_platform() -> None:
    with pytest.raises(PhysicalTrainingPilotError, match="Windows"):
        PhysicalTrainingPilotReport(
            job_id="pilot-job",
            base_sha256="a" * 64,
            frozen_package_sha256="b" * 64,
            training_material_sha256="c" * 64,
            scale_authorization_sha256="d" * 64,
            execution_plan_sha256="e" * 64,
            job_fingerprint="f" * 64,
            paused_checkpoint_id="checkpoint-paused",
            completed_checkpoint_id="checkpoint-completed",
            candidate_artifact_ref="models/candidate/pilot",
            candidate_sha256="1" * 64,
            candidate_byte_count=10,
            completed_steps=2,
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
        )
