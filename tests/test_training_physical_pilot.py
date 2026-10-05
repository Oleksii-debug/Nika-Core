from __future__ import annotations

import hashlib
import json
import os
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
    write_physical_training_pilot_report,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
)


_TRAINER_JOB_FINGERPRINT = "9" * 64
_TRAINER_DEPLOYMENT_IDENTITY = ArtifactIdentity("3" * 64, "5" * 64)


@pytest.fixture(autouse=True)
def _windows_report_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nika_core.training_physical_pilot._is_windows",
        lambda: True,
    )
    monkeypatch.setattr(
        pilot,
        "candidate_adapter_manifest",
        lambda _: _candidate_manifest(),
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate_manifest() -> dict[str, object]:
    return {
        "adapter_config": {
            "base_model_name_or_path": "models/base",
            "bias": "none",
            "lora_alpha": 16,
            "lora_dropout": 0.0,
            "r": 8,
            "target_modules": ["q_proj"],
            "task_type": "CAUSAL_LM",
        },
        "base_artifact_ref": "models/base",
        "base_artifact_sha256": "a" * 64,
        "candidate_artifact_ref": "models/candidate/pilot",
        "consumed_materials_sha256": "1" * 64,
        "job_fingerprint": _TRAINER_JOB_FINGERPRINT,
        "model_dir_manifest_sha256": "2" * 64,
        "trainer_artifact_id": "3" * 64,
        "trainer_implementation_sha256": "4" * 64,
        "trainer_sha256": "5" * 64,
        "training_runtime_manifest_sha256": "6" * 64,
        "training_runtime_versions": {
            "accelerate": "1.0",
            "gguf": "1.0",
            "peft": "1.0",
            "safetensors": "1.0",
            "torch": "1.0",
            "transformers": "1.0",
        },
        "schema": "nika-peft-candidate-v1",
        "step_number": 2,
        "trainer_parameters": {
            "learning_rate": 0.0002,
            "lora_alpha": 16,
            "lora_dropout": 0.0,
            "lora_r": 8,
            "lora_target_modules": ["q_proj"],
            "max_records": 100,
            "max_sequence_length": 64,
            "seed": 1,
            "torch_num_threads": 1,
        },
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
        trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
        trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
        trainer_consumed_materials_sha256="1" * 64,
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
        trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
        trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
        trainer_consumed_materials_sha256="1" * 64,
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
    assert report.schema_version == 4
    assert report.completed_steps == 2
    assert report.job_fingerprint == "f" * 64
    assert report.trainer_job_fingerprint == _TRAINER_JOB_FINGERPRINT
    assert report.job_fingerprint != report.trainer_job_fingerprint
    assert report.consumed_materials_sha256 == "1" * 64
    assert report.model_dir_manifest_sha256 == "2" * 64
    assert report.trainer_artifact_id == "3" * 64
    assert report.trainer_implementation_sha256 == "4" * 64
    assert report.trainer_deployment_sha256 == "5" * 64
    assert report.training_runtime_manifest_sha256 == "6" * 64
    assert len(report.candidate_manifest_sha256) == 64
    assert report.candidate_sha256 == _sha256(payload)
    assert report.candidate_byte_count == len(payload)
    assert report.candidate_descriptor_sha256 == descriptor.descriptor_digest
    assert report.candidate_registry_key == descriptor.registry_key
    assert report.paused_checkpoint_id == "checkpoint-paused"
    assert report.restart_checkpoint_id == "checkpoint-restart"
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


def test_report_from_json_rejects_bytes_transport(tmp_path: Path) -> None:
    report = _build_report(tmp_path)

    with pytest.raises(TypeError, match="exact text"):
        PhysicalTrainingPilotReport.from_json(
            report.to_json().encode("utf-8"),  # type: ignore[arg-type]
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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


def test_build_report_rejects_candidate_manifest_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    manifest = _candidate_manifest()
    manifest["job_fingerprint"] = "0" * 64
    monkeypatch.setattr(pilot, "candidate_adapter_manifest", lambda _: manifest)

    with pytest.raises(PhysicalTrainingPilotError, match="job fingerprint"):
        build_physical_training_pilot_report(
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("trainer_artifact_id", "7" * 64, "trainer artifact identity"),
        ("trainer_sha256", "8" * 64, "trainer deployment digest"),
    ),
)
def test_build_report_rejects_candidate_manifest_trainer_deployment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    manifest = _candidate_manifest()
    manifest[field] = value
    monkeypatch.setattr(pilot, "candidate_adapter_manifest", lambda _: manifest)

    with pytest.raises(PhysicalTrainingPilotError, match=message):
        build_physical_training_pilot_report(
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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


def test_build_report_rejects_candidate_manifest_consumed_material_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    manifest = _candidate_manifest()
    manifest["consumed_materials_sha256"] = "0" * 64
    monkeypatch.setattr(pilot, "candidate_adapter_manifest", lambda _: manifest)

    with pytest.raises(PhysicalTrainingPilotError, match="consumed-material attestation"):
        build_physical_training_pilot_report(
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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


def test_build_report_rejects_candidate_manifest_reader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)

    def fail_reader(_: Path) -> dict[str, object]:
        raise pilot.PeftTrainerError("candidate_manifest_invalid")

    monkeypatch.setattr(pilot, "candidate_adapter_manifest", fail_reader)

    with pytest.raises(PhysicalTrainingPilotError, match="manifest verification failed"):
        build_physical_training_pilot_report(
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics required")
def test_build_report_holds_candidate_stable_during_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"candidate"
    candidate = tmp_path / "adapter_model.safetensors"
    candidate.write_bytes(payload)
    replacement = tmp_path / "replacement.safetensors"
    replacement.write_bytes(b"replacement")
    blocked: list[int | None] = []

    def racing_reader(path: Path) -> dict[str, object]:
        try:
            os.replace(replacement, path)
        except OSError as exc:
            blocked.append(getattr(exc, "winerror", None))
        else:
            raise AssertionError("candidate replacement must be blocked during evidence read")
        return _candidate_manifest()

    monkeypatch.setattr(pilot, "candidate_adapter_manifest", racing_reader)

    report = build_physical_training_pilot_report(
        trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
        trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
        trainer_consumed_materials_sha256="1" * 64,
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

    assert report.candidate_sha256 == _sha256(payload)
    assert blocked
    assert replacement.exists()
    os.replace(replacement, candidate)
    assert candidate.read_bytes() == b"replacement"


def test_report_publication_round_trips_exact_canonical_bytes(tmp_path: Path) -> None:
    report = _build_report(tmp_path)
    output = (tmp_path / "evidence.json").resolve()

    write_physical_training_pilot_report(report, output)

    payload = output.read_bytes()
    assert payload == report.to_json().encode("utf-8")
    restored = PhysicalTrainingPilotReport.from_json(
        payload.decode("utf-8", errors="strict")
    )
    assert restored == report


def test_report_publication_refuses_to_clobber_existing_evidence(tmp_path: Path) -> None:
    report = _build_report(tmp_path)
    output = (tmp_path / "evidence.json").resolve()
    output.write_bytes(b"existing-evidence")

    with pytest.raises(PhysicalTrainingPilotError, match="already exists"):
        write_physical_training_pilot_report(report, output)

    assert output.read_bytes() == b"existing-evidence"


def test_report_publication_cleans_temporary_file_on_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _build_report(tmp_path)
    output = (tmp_path / "evidence.json").resolve()
    original_link = os.link

    def fail_link(_: object, __: object) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(PhysicalTrainingPilotError, match="published atomically"):
        write_physical_training_pilot_report(report, output)

    monkeypatch.setattr(os, "link", original_link)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".evidence.json.*.tmp"))


def test_report_publication_removes_destination_if_parse_back_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _build_report(tmp_path)
    output = (tmp_path / "evidence.json").resolve()

    def reject_parse_back(
        cls: type[PhysicalTrainingPilotReport],
        raw: str,
    ) -> PhysicalTrainingPilotReport:
        del cls, raw
        raise PhysicalTrainingPilotError("simulated parse-back failure")

    monkeypatch.setattr(
        PhysicalTrainingPilotReport,
        "from_json",
        classmethod(reject_parse_back),
    )

    with pytest.raises(PhysicalTrainingPilotError, match="parse-back failure"):
        write_physical_training_pilot_report(report, output)

    assert not output.exists()
    assert not tuple(tmp_path.glob(".evidence.json.*.tmp"))


def test_report_publication_requires_absolute_canonical_parent(tmp_path: Path) -> None:
    report = _build_report(tmp_path)

    with pytest.raises(PhysicalTrainingPilotError, match="absolute canonical"):
        write_physical_training_pilot_report(report, Path("evidence.json"))


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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=_TRAINER_JOB_FINGERPRINT,
            trainer_deployment_identity=_TRAINER_DEPLOYMENT_IDENTITY,
            trainer_consumed_materials_sha256="1" * 64,
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
            trainer_job_fingerprint=report.trainer_job_fingerprint,
            paused_checkpoint_id=report.paused_checkpoint_id,
            restart_checkpoint_id=report.restart_checkpoint_id,
            completed_checkpoint_id=report.completed_checkpoint_id,
            candidate_artifact_ref=report.candidate_artifact_ref,
            candidate_descriptor_sha256=report.candidate_descriptor_sha256,
            candidate_registry_key=report.candidate_registry_key,
            candidate_sha256=report.candidate_sha256,
            candidate_byte_count=report.candidate_byte_count,
            candidate_manifest_sha256=report.candidate_manifest_sha256,
            consumed_materials_sha256=report.consumed_materials_sha256,
            model_dir_manifest_sha256=report.model_dir_manifest_sha256,
            trainer_artifact_id=report.trainer_artifact_id,
            trainer_deployment_sha256=report.trainer_deployment_sha256,
            trainer_implementation_sha256=report.trainer_implementation_sha256,
            training_runtime_manifest_sha256=report.training_runtime_manifest_sha256,
            completed_steps=report.completed_steps,
            platform="linux",
        )


def _install_runner_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resumed_probe: TrainingRunEvidence,
    completed: TrainingRunEvidence,
    initial_pause: TrainingRunEvidence | None = None,
    initial_trainer_job_fingerprint: str = _TRAINER_JOB_FINGERPRINT,
    resumed_trainer_job_fingerprint: str = _TRAINER_JOB_FINGERPRINT,
    initial_trainer_deployment_identity: ArtifactIdentity = _TRAINER_DEPLOYMENT_IDENTITY,
    resumed_trainer_deployment_identity: ArtifactIdentity = _TRAINER_DEPLOYMENT_IDENTITY,
    initial_consumed_materials_sha256: str = "1" * 64,
    resumed_consumed_materials_sha256: str = "1" * 64,
    initial_worker_preaccepted_sha256: str | None = None,
) -> tuple[object, object, list[tuple[str, bool]]]:
    calls: list[tuple[str, bool]] = []

    class FakeRuntime:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        def run(self, *args: object, control: object = None, **__: object) -> TrainingRunEvidence:
            self.calls += 1
            calls.append((self.name, control is not None))
            assert len(args) >= 2
            worker_arg = args[1]
            assert isinstance(worker_arg, FakeWorker)
            if self.name == "initial":
                worker_arg.accepted_consumed_materials_sha256 = (
                    initial_consumed_materials_sha256
                )
                if initial_pause is not None:
                    return initial_pause
                return _run_evidence(
                    state=TrainingRunState.PAUSED,
                    next_step=1,
                    checkpoint_id="checkpoint-paused",
                )
            if self.calls == 1:
                return resumed_probe
            worker_arg.accepted_consumed_materials_sha256 = (
                resumed_consumed_materials_sha256
            )
            return completed

    class FakeWorker:
        def __init__(
            self,
            trainer_job_fingerprint: str,
            trainer_deployment_identity: ArtifactIdentity,
        ) -> None:
            self.trainer_job_fingerprint = trainer_job_fingerprint
            self.trainer_deployment_identity = trainer_deployment_identity
            self.accepted_consumed_materials_sha256: str | None = None

        @property
        def execution_plan_sha256(self) -> str:
            return "e" * 64

        def protocol_job_fingerprint(self, _: object) -> str:
            return self.trainer_job_fingerprint

        def verified_trainer_deployment_identity(self) -> ArtifactIdentity:
            return self.trainer_deployment_identity

        @property
        def last_accepted_consumed_materials_sha256(self) -> str | None:
            return self.accepted_consumed_materials_sha256

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
    initial_worker = FakeWorker(
        initial_trainer_job_fingerprint,
        initial_trainer_deployment_identity,
    )
    initial_worker.accepted_consumed_materials_sha256 = (
        initial_worker_preaccepted_sha256
    )
    resumed_worker = FakeWorker(
        resumed_trainer_job_fingerprint,
        resumed_trainer_deployment_identity,
    )
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
    def fake_build_report(**kwargs: object) -> object:
        assert kwargs["trainer_job_fingerprint"] == resumed_trainer_job_fingerprint
        assert (
            kwargs["trainer_deployment_identity"]
            == resumed_trainer_deployment_identity
        )
        assert (
            kwargs["trainer_consumed_materials_sha256"]
            == resumed_consumed_materials_sha256
        )
        return sentinel

    monkeypatch.setattr(
        pilot,
        "build_physical_training_pilot_report",
        fake_build_report,
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


def test_physical_runner_rejects_preused_initial_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        PhysicalTrainingPilotError,
        match="initial worker already carries accepted consumed-material evidence",
    ):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
            initial_worker_preaccepted_sha256="9" * 64,
        )


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


def test_physical_runner_rejects_trainer_protocol_identity_drift_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PhysicalTrainingPilotError, match="protocol job identity"):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
            initial_trainer_job_fingerprint="7" * 64,
            resumed_trainer_job_fingerprint="8" * 64,
        )


def test_physical_runner_rejects_consumed_material_attestation_drift_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        PhysicalTrainingPilotError,
        match="accepted consumed-material attestation changed across restart",
    ):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
            initial_consumed_materials_sha256="1" * 64,
            resumed_consumed_materials_sha256="2" * 64,
        )


def test_physical_runner_rejects_trainer_deployment_drift_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PhysicalTrainingPilotError, match="deployment identity"):
        _install_runner_fakes(
            monkeypatch,
            resumed_probe=_restart_probe(),
            completed=_completed_for(b"candidate"),
            initial_trainer_deployment_identity=ArtifactIdentity("3" * 64, "5" * 64),
            resumed_trainer_deployment_identity=ArtifactIdentity("7" * 64, "8" * 64),
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
