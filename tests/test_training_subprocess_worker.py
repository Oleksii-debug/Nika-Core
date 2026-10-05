from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.training_adapters import SubprocessTrainingWorker, TrainingSubprocessError
from nika_core.training_materials import ResolvedTrainingPackage, resolve_training_materials
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingWorkerFailureEffect,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _script(tmp_path: Path, body: str, *, name: str = "trainer.py") -> Path:
    path = (tmp_path / name).resolve()
    path.write_text(body, encoding="utf-8")
    return path


def _materials(tmp_path: Path) -> ResolvedTrainingPackage:
    workspace_id = "workspace-alpha"
    store = ContentAddressedBlobStore(tmp_path / "training-blobs")
    training_body = b'{"prompt":"a","response":"b"}\n'
    validation_body = b'{"prompt":"c","response":"d"}\n'
    store.put_bytes(workspace_id, training_body)
    store.put_bytes(workspace_id, validation_body)
    training = LearningShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(training_body),
        provenance_sha256=_sha256(b"training-provenance"),
        license_evidence_sha256=_sha256(b"training-license"),
        record_count=1,
        byte_count=len(training_body),
    )
    validation = LearningShard(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(validation_body),
        provenance_sha256=_sha256(b"validation-provenance"),
        license_evidence_sha256=_sha256(b"validation-license"),
        record_count=1,
        byte_count=len(validation_body),
    )
    package = FrozenLearningPackage.freeze(
        package_id="pkg-1",
        package_version="1",
        base_artifact_sha256="a" * 64,
        selection_policy_sha256=_sha256(b"selection-policy"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=_sha256(b"strict-held-out-evaluation"),
        shards=(training, validation),
    )
    return resolve_training_materials(
        package,
        workspace_id=workspace_id,
        blob_store=store,
    )


def _spec(
    materials: ResolvedTrainingPackage,
    *,
    max_steps: int = 3,
    job_id: str = "job-1",
) -> TrainingJobSpec:
    return TrainingJobSpec(
        job_id=job_id,
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity(
            "models/base",
            materials.evidence.base_artifact_sha256,
        ),
        frozen_package_sha256=materials.evidence.package_manifest_sha256,
        training_material_sha256=materials.training_material_sha256,
        candidate_artifact_ref=f"models/candidate/{job_id}",
        max_steps=max_steps,
    )


def _worker(
    tmp_path: Path,
    trainer: Path,
    *,
    command: tuple[str, ...] | None = None,
    environment: dict[str, str] | None = None,
    timeout_seconds: float = 300.0,
    max_request_bytes: int = 64 * 1024,
    max_response_bytes: int = 64 * 1024,
) -> SubprocessTrainingWorker:
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "artifact-registry.sqlite3"),
        local_file_roots=(tmp_path,),
    )
    record = registry.register_file(
        workspace_id="workspace-alpha",
        idempotency_key=f"trainer:{trainer.name}:{_sha256(trainer.read_bytes())}",
        path=trainer,
        kind="training_worker",
        producer_type="test",
    )
    return SubprocessTrainingWorker(
        command or (sys.executable, str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
        environment=environment,
        timeout_seconds=timeout_seconds,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
    )


def _invoke(
    worker: SubprocessTrainingWorker,
    tmp_path: Path,
    *,
    step_index: int,
    resume_state: dict[str, object],
    spec: TrainingJobSpec | None = None,
) -> object:
    materials = _materials(tmp_path)
    return worker.step(
        spec=spec or _spec(materials),
        step_index=step_index,
        resume_state=resume_state,
        training_materials=materials,
    )


def test_real_subprocess_step_resume_and_completion(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
step_index = request["step_index"]
response = {
    "candidate_sha256": "b" * 64 if step_index == 1 else None,
    "completed": step_index == 1,
    "protocol_version": 1,
    "resume_state": {
        "next_epoch": step_index + 1,
        "material_count": len(request["training_materials"]),
        "material_digest": request["job"]["training_material_sha256"],
    },
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    first = _invoke(worker, tmp_path, step_index=0, resume_state={})
    assert first.completed is False
    assert first.candidate_sha256 is None
    first_envelope = first.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    state = first_envelope["trainer_state"]
    assert isinstance(state, dict)
    assert state["material_count"] == 2
    assert state["material_digest"] == _materials(tmp_path).training_material_sha256

    second = _invoke(
        worker,
        tmp_path,
        step_index=1,
        resume_state=first.resume_state,
    )
    assert second.completed is True
    assert second.candidate_sha256 == "b" * 64


def test_step_identity_is_stable_for_replay_and_registry_bound(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {
        "observed_step_id": request["step_id"],
        "trainer_artifact_id": request["trainer_artifact_id"],
    },
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    first = _invoke(worker, tmp_path, step_index=0, resume_state={})
    replay = _invoke(worker, tmp_path, step_index=0, resume_state={})

    first_envelope = first.resume_state["_nika_subprocess"]
    replay_envelope = replay.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    assert isinstance(replay_envelope, dict)
    assert first_envelope["last_step_id"] == replay_envelope["last_step_id"]
    assert first_envelope["trainer_artifact_id"] == replay_envelope["trainer_artifact_id"]
    assert len(str(first_envelope["trainer_sha256"])) == 64


def test_resume_rejects_different_registered_trainer_before_process_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "second-step-started"
    body = f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
if request["step_index"] == 1:
    Path({str(marker)!r}).write_text("started", encoding="utf-8")
response = {{
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {{"next_epoch": request["step_index"] + 1}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip()
    trainer = _script(tmp_path, body, name="trainer-a.py")
    original = _worker(tmp_path, trainer)
    first = _invoke(original, tmp_path, step_index=0, resume_state={})

    replacement_path = _script(
        tmp_path,
        body + "\n# distinct registered trainer\n",
        name="trainer-b.py",
    )
    replacement = _worker(tmp_path, replacement_path)

    with pytest.raises(TrainingSubprocessError, match="trainer registry artifact") as exc_info:
        _invoke(
            replacement,
            tmp_path,
            step_index=1,
            resume_state=first.resume_state,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_registry_tamper_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    worker = _worker(tmp_path, trainer)
    original = trainer.read_bytes()
    trainer.write_bytes(b"x" * len(original))

    with pytest.raises(TrainingSubprocessError, match="physical verification") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_parent_environment_is_not_inherited_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_TRAINING_SECRET", "must-not-leak")
    trainer = _script(
        tmp_path,
        """
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"secret_seen": os.getenv("NIKA_TRAINING_SECRET") is not None},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    result = _invoke(worker, tmp_path, step_index=0, resume_state={})
    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["secret_seen"] is False


def test_explicit_environment_is_the_only_environment_exposed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"allowed": os.getenv("NIKA_ALLOWED")},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(
        tmp_path,
        trainer,
        environment={"NIKA_ALLOWED": "yes"},
    )

    result = _invoke(worker, tmp_path, step_index=0, resume_state={})
    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["allowed"] == "yes"


def test_nonzero_exit_is_secret_minimized_and_effect_unknown(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys
sys.stderr.write("TOP-SECRET-TRAINING-DATA")
raise SystemExit(9)
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert "TOP-SECRET-TRAINING-DATA" not in str(exc_info.value)
    assert "9" in str(exc_info.value)
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_timeout_is_bounded_minimized_and_effect_unknown(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import time
time.sleep(10)
""".strip(),
    )
    worker = _worker(tmp_path, trainer, timeout_seconds=0.1)

    with pytest.raises(TrainingSubprocessError, match="timed out") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_oversized_response_is_rejected_during_execution(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys

sys.stdin.buffer.read()
chunk = b"x" * 65536
while True:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
""".strip(),
    )
    worker = _worker(
        tmp_path,
        trainer,
        max_response_bytes=1024,
        timeout_seconds=5,
    )

    with pytest.raises(TrainingSubprocessError, match="response exceeds") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_wrong_step_identity_fails_closed_as_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": "0" * 64,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError, match="wrong step identity") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_unknown_response_field_fails_closed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": request["step_id"],
    "unexpected": True,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError, match="unexpected fields") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_invalid_candidate_digest_is_minimized(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": "not-a-digest",
    "completed": True,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError, match="invalid result evidence") as exc_info:
        _invoke(worker, tmp_path, step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_invalid_resume_state_is_rejected_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    worker = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError, match="non-JSON") as exc_info:
        materials = _materials(tmp_path)
        worker.step(
            spec=_spec(materials),
            step_index=1,
            resume_state={"bad": object()},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_material_identity_mismatch_is_rejected_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    worker = _worker(tmp_path, trainer)
    materials = _materials(tmp_path)
    spec = TrainingJobSpec(
        job_id="job-1",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity("models/base", materials.evidence.base_artifact_sha256),
        frozen_package_sha256=materials.evidence.package_manifest_sha256,
        training_material_sha256="f" * 64,
        candidate_artifact_ref="models/candidate/job-1",
        max_steps=3,
    )

    with pytest.raises(TrainingSubprocessError, match="material identity") as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_resume_state_cannot_cross_jobs(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"position": 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, trainer)
    materials = _materials(tmp_path)
    first = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    changed = _spec(materials, job_id="job-2")

    with pytest.raises(TrainingSubprocessError, match="current job") as exc_info:
        worker.step(
            spec=changed,
            step_index=1,
            resume_state=first.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT


def test_command_must_not_be_a_shell_string(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "print('unused')")
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "registry.sqlite3"),
        local_file_roots=(tmp_path,),
    )
    record = registry.register_file(
        workspace_id="workspace-alpha",
        idempotency_key="trainer",
        path=trainer,
        kind="training_worker",
    )
    with pytest.raises(TypeError, match="not a shell string"):
        SubprocessTrainingWorker(
            "python trainer.py",
            artifact_registry=registry,
            trainer_artifact_id=record.artifact_id,
        )


def test_training_executable_must_be_absolute(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "print('unused')")
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "registry.sqlite3"),
        local_file_roots=(tmp_path,),
    )
    record = registry.register_file(
        workspace_id="workspace-alpha",
        idempotency_key="trainer",
        path=trainer,
        kind="training_worker",
    )
    with pytest.raises(ValueError, match="absolute path"):
        SubprocessTrainingWorker(
            ("python", str(trainer)),
            artifact_registry=registry,
            trainer_artifact_id=record.artifact_id,
        )


def test_caller_supplied_trainer_digest_is_not_an_adapter_api(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "print('unused')")
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "registry.sqlite3"),
        local_file_roots=(tmp_path,),
    )
    record = registry.register_file(
        workspace_id="workspace-alpha",
        idempotency_key="trainer",
        path=trainer,
        kind="training_worker",
    )
    with pytest.raises(TypeError):
        SubprocessTrainingWorker(
            (sys.executable, str(trainer)),
            artifact_registry=registry,
            trainer_artifact_id=record.artifact_id,
            trainer_sha256="c" * 64,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, 0.0, True])
def test_invalid_timeout_is_rejected(tmp_path: Path, timeout: object) -> None:
    trainer = _script(tmp_path, "print('unused')")
    with pytest.raises(ValueError, match="timeout_seconds"):
        _worker(
            tmp_path,
            trainer,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_huge_integer_timeout_is_rejected_without_overflow(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "print('unused')")
    with pytest.raises(ValueError, match="timeout_seconds"):
        _worker(
            tmp_path,
            trainer,
            timeout_seconds=10**10000,  # type: ignore[arg-type]
        )
