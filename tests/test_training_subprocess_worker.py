from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.training_adapters import SubprocessTrainingWorker, TrainingSubprocessError
from nika_core.training_materials import (
    ResolvedTrainingPackage,
    TrainingMaterialEvidence,
    TrainingMaterialSetEvidence,
    resolve_training_materials,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingStepResult,
    TrainingWorkerFailureEffect,
)

_TRAINING_BODY = b'{"prompt":"train","response":"ok"}\n'
_VALIDATION_BODY = b'{"prompt":"validate","response":"ok"}\n'


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "trainer.py"
    path.write_text(body, encoding="utf-8")
    return path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _frozen_package() -> FrozenLearningPackage:
    training = LearningShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(_TRAINING_BODY),
        provenance_sha256=_sha256(b"training-provenance"),
        license_evidence_sha256=_sha256(b"training-license"),
        record_count=1,
        byte_count=len(_TRAINING_BODY),
    )
    validation = LearningShard(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(_VALIDATION_BODY),
        provenance_sha256=_sha256(b"validation-provenance"),
        license_evidence_sha256=_sha256(b"validation-license"),
        record_count=1,
        byte_count=len(_VALIDATION_BODY),
    )
    return FrozenLearningPackage.freeze(
        package_id="subprocess-package",
        package_version="1",
        base_artifact_sha256=_sha256(b"base-model"),
        selection_policy_sha256=_sha256(b"selection-policy"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=_sha256(b"held-out"),
        shards=(training, validation),
    )


def _material_evidence() -> TrainingMaterialSetEvidence:
    package = _frozen_package()
    return TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha256(b"subprocess-training"),
        materials=tuple(
            TrainingMaterialEvidence.from_shard(shard) for shard in package.shards
        ),
    )


def _resolved_materials(tmp_path: Path) -> ResolvedTrainingPackage:
    blob_store = ContentAddressedBlobStore(tmp_path / "material-blobs")
    blob_store.put_bytes("subprocess-training", _TRAINING_BODY)
    blob_store.put_bytes("subprocess-training", _VALIDATION_BODY)
    return resolve_training_materials(
        _frozen_package(),
        workspace_id="subprocess-training",
        blob_store=blob_store,
    )


def _artifact_registry(tmp_path: Path) -> ArtifactRegistry:
    executable_root = Path(sys.executable).resolve().parent
    return ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "artifact-registry.sqlite3"),
        local_file_roots=(executable_root, tmp_path),
    )


def _worker(
    tmp_path: Path,
    command: tuple[str, ...],
    *,
    idempotency_key: str = "trainer-python",
    **kwargs: object,
) -> SubprocessTrainingWorker:
    registry = _artifact_registry(tmp_path)
    record = registry.register_file(
        workspace_id="training",
        idempotency_key=idempotency_key,
        path=command[0],
        kind="training_executable",
    )
    return SubprocessTrainingWorker(
        command,
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
        **kwargs,  # type: ignore[arg-type]
    )


def _step(
    worker: SubprocessTrainingWorker,
    tmp_path: Path,
    *,
    spec: TrainingJobSpec,
    step_index: int,
    resume_state: dict[str, object],
) -> TrainingStepResult:
    return worker.step(
        spec=spec,
        step_index=step_index,
        resume_state=resume_state,
        training_materials=_resolved_materials(tmp_path),
    )


def _spec(*, max_steps: int = 3) -> TrainingJobSpec:
    package = _frozen_package()
    return TrainingJobSpec(
        job_id="job-1",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity("models/base", package.base_artifact_sha256),
        frozen_package_sha256=package.manifest_sha256,
        training_material_sha256=_material_evidence().training_material_sha256,
        candidate_artifact_ref="models/candidate/job-1",
        max_steps=max_steps,
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
    "protocol_version": 2,
    "resume_state": {"next_epoch": step_index + 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    first = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})
    assert first.completed is False
    assert first.candidate_sha256 is None

    second = _step(worker, tmp_path, spec=_spec(), step_index=1, resume_state=first.resume_state)
    assert second.completed is True
    assert second.candidate_sha256 == "b" * 64


def test_request_binds_exact_material_paths_and_digests(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read())
materials = request["training_materials"]
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {
        "all_paths_absolute": all(os.path.isabs(item["path"]) for item in materials["materials"]),
        "count": len(materials["materials"]),
        "package_manifest_sha256": materials["package_manifest_sha256"],
        "training_material_sha256": materials["training_material_sha256"],
    },
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))
    spec = _spec()

    result = _step(worker, tmp_path, spec=spec, step_index=0, resume_state={})

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["all_paths_absolute"] is True
    assert trainer_state["count"] == 2
    assert trainer_state["package_manifest_sha256"] == spec.frozen_package_sha256
    assert trainer_state["training_material_sha256"] == spec.training_material_sha256


def test_resume_rejects_changed_command_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "changed-command-started"
    trainer = _script(
        tmp_path,
        f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
if request["step_index"] == 1:
    Path({str(marker)!r}).write_text("started", encoding="utf-8")
response = {{
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {{}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    first_worker = _worker(
        tmp_path,
        (sys.executable, str(trainer), "mode-a"),
        idempotency_key="trainer-command",
    )
    first = _step(first_worker, tmp_path, spec=_spec(), step_index=0, resume_state={})
    changed_worker = _worker(
        tmp_path,
        (sys.executable, str(trainer), "mode-b"),
        idempotency_key="trainer-command",
    )

    with pytest.raises(TrainingSubprocessError, match="trainer artifact") as exc_info:
        _step(
            changed_worker,
            tmp_path,
            spec=_spec(),
            step_index=1,
            resume_state=first.resume_state,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_registry_path_mismatch_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "registry-mismatch-started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    registry = _artifact_registry(tmp_path)
    record = registry.register_file(
        workspace_id="training",
        idempotency_key="wrong-trainer-artifact",
        path=trainer,
        kind="training_executable",
    )
    worker = SubprocessTrainingWorker(
        (sys.executable, str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
    )

    with pytest.raises(TrainingSubprocessError, match="registered trainer artifact") as exc_info:
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_step_identity_is_stable_for_replay(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {"observed_step_id": request["step_id"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    first = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})
    replay = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

    first_envelope = first.resume_state["_nika_subprocess"]
    replay_envelope = replay.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    assert isinstance(replay_envelope, dict)
    assert first_envelope["last_step_id"] == replay_envelope["last_step_id"]
    assert first_envelope["trainer_sha256"] == replay_envelope["trainer_sha256"]
    assert first_envelope["trainer_artifact_id"] == replay_envelope["trainer_artifact_id"]
    assert first_envelope["trainer_command_sha256"] == replay_envelope["trainer_command_sha256"]


def test_resume_rejects_different_trainer_artifact_before_process_effect(tmp_path: Path) -> None:
    second_step_marker = tmp_path / "second-step-started"
    trainer = _script(
        tmp_path,
        f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
if request["step_index"] == 1:
    Path({str(second_step_marker)!r}).write_text("started", encoding="utf-8")
response = {{
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {{"next_epoch": request["step_index"] + 1}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    original = _worker(
        tmp_path,
        (sys.executable, str(trainer)),
        idempotency_key="trainer-original",
    )
    first = _step(original, tmp_path, spec=_spec(), step_index=0, resume_state={})
    replacement = _worker(
        tmp_path,
        (sys.executable, str(trainer)),
        idempotency_key="trainer-replacement",
    )

    with pytest.raises(TrainingSubprocessError, match="trainer artifact"):
        _step(replacement, tmp_path, spec=_spec(), step_index=1, resume_state=first.resume_state)

    assert not second_step_marker.exists()


def test_parent_environment_is_not_inherited_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    "protocol_version": 2,
    "resume_state": {"secret_seen": os.getenv("NIKA_TRAINING_SECRET") is not None},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    result = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

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
    "protocol_version": 2,
    "resume_state": {"allowed": os.getenv("NIKA_ALLOWED")},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(
        tmp_path,
        (sys.executable, str(trainer)),
        environment={"NIKA_ALLOWED": "yes"},
    )

    result = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["allowed"] == "yes"


def test_nonzero_exit_does_not_expose_stderr(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys
sys.stderr.write("TOP-SECRET-TRAINING-DATA")
raise SystemExit(9)
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError) as exc_info:
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

    assert "TOP-SECRET-TRAINING-DATA" not in str(exc_info.value)
    assert "9" in str(exc_info.value)
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_timeout_is_bounded_and_minimized(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import time
time.sleep(10)
""".strip(),
    )
    worker = _worker(
        tmp_path,
        (sys.executable, str(trainer)),
        timeout_seconds=0.1,
    )

    with pytest.raises(TrainingSubprocessError, match="timed out") as exc_info:
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})

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
        (sys.executable, str(trainer)),
        max_response_bytes=1024,
        timeout_seconds=5,
    )

    with pytest.raises(TrainingSubprocessError, match="response exceeds"):
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})


def test_wrong_step_identity_fails_closed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {},
    "step_id": "0" * 64,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="wrong step identity"):
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})


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
    "protocol_version": 2,
    "resume_state": {},
    "step_id": request["step_id"],
    "unexpected": True,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="unexpected fields"):
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})


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
    "protocol_version": 2,
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="invalid result evidence"):
        _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})


def test_invalid_resume_state_is_rejected_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="non-JSON") as exc_info:
        _step(
            worker,
            tmp_path,
            spec=_spec(),
            step_index=1,
            resume_state={"bad": object()},
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
    "protocol_version": 2,
    "resume_state": {"position": 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = _worker(tmp_path, (sys.executable, str(trainer)))
    first = _step(worker, tmp_path, spec=_spec(), step_index=0, resume_state={})
    changed = TrainingJobSpec(
        job_id="job-2",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity(
            "models/base", _frozen_package().base_artifact_sha256
        ),
        frozen_package_sha256=_frozen_package().manifest_sha256,
        training_material_sha256=_material_evidence().training_material_sha256,
        candidate_artifact_ref="models/candidate/job-2",
        max_steps=3,
    )

    with pytest.raises(TrainingSubprocessError, match="does not match the current job"):
        _step(worker, tmp_path, spec=changed, step_index=1, resume_state=first.resume_state)


def test_command_must_not_be_a_shell_string() -> None:
    with pytest.raises(TypeError, match="not a shell string"):
        SubprocessTrainingWorker(
            "python trainer.py",
            artifact_registry=None,  # type: ignore[arg-type]
            trainer_artifact_id="a" * 64,
        )


def test_training_executable_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        SubprocessTrainingWorker(
            ("python", "trainer.py"),
            artifact_registry=None,  # type: ignore[arg-type]
            trainer_artifact_id="a" * 64,
        )


def test_trainer_artifact_identity_must_be_exact_sha256(tmp_path: Path) -> None:
    registry = _artifact_registry(tmp_path)
    with pytest.raises(ValueError, match="trainer_artifact_id"):
        SubprocessTrainingWorker(
            (os.path.abspath(sys.executable),),
            artifact_registry=registry,
            trainer_artifact_id="not-a-digest",
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, 0.0, True])
def test_invalid_timeout_is_rejected(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker(
            (os.path.abspath(sys.executable),),
            artifact_registry=None,  # type: ignore[arg-type]
            trainer_artifact_id="a" * 64,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_huge_integer_timeout_is_rejected_without_overflow() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker(
            (os.path.abspath(sys.executable),),
            artifact_registry=None,  # type: ignore[arg-type]
            trainer_artifact_id="a" * 64,
            timeout_seconds=10**10000,  # type: ignore[arg-type]
        )
