from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager
from nika_core.training_adapters import SubprocessTrainingWorker
from nika_core.training_materials import ResolvedTrainingPackage, resolve_training_materials
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunState,
    TrainingRuntime,
    TrainingWorkerError,
    TrainingWorkerFailureEffect,
)

_WORKSPACE_ID = "subprocess-training"
_TRAINING_BODY = b'{"prompt":"train","response":"ok"}\n'
_VALIDATION_BODY = b'{"prompt":"validate","response":"ok"}\n'


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _package() -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="subprocess-package",
        package_version="1",
        base_artifact_sha256=_sha256(b"base-model"),
        selection_policy_sha256=_sha256(b"selection-policy"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=_sha256(b"held-out"),
        shards=(
            LearningShard(
                split=LearningDataSplit.TRAINING,
                artifact_sha256=_sha256(_TRAINING_BODY),
                provenance_sha256=_sha256(b"training-provenance"),
                license_evidence_sha256=_sha256(b"training-license"),
                record_count=1,
                byte_count=len(_TRAINING_BODY),
            ),
            LearningShard(
                split=LearningDataSplit.VALIDATION,
                artifact_sha256=_sha256(_VALIDATION_BODY),
                provenance_sha256=_sha256(b"validation-provenance"),
                license_evidence_sha256=_sha256(b"validation-license"),
                record_count=1,
                byte_count=len(_VALIDATION_BODY),
            ),
        ),
    )


def _materials(tmp_path: Path) -> ResolvedTrainingPackage:
    blobs = ContentAddressedBlobStore(tmp_path / "material-blobs")
    blobs.put_bytes(_WORKSPACE_ID, _TRAINING_BODY)
    blobs.put_bytes(_WORKSPACE_ID, _VALIDATION_BODY)
    return resolve_training_materials(
        _package(),
        workspace_id=_WORKSPACE_ID,
        blob_store=blobs,
    )


def _spec(
    materials: ResolvedTrainingPackage,
    *,
    max_steps: int = 3,
    **overrides: object,
) -> TrainingJobSpec:
    package = _package()
    values: dict[str, object] = {
        "job_id": "job-1",
        "task_id": "training-task",
        "project_id": "project-1",
        "owner_id": "owner-1",
        "base_artifact": ArtifactIdentity(
            artifact_ref="models/base",
            sha256=package.base_artifact_sha256,
        ),
        "frozen_package_sha256": package.manifest_sha256,
        "training_material_sha256": materials.training_material_sha256,
        "candidate_artifact_ref": "models/candidate/job-1",
        "max_steps": max_steps,
    }
    values.update(overrides)
    return TrainingJobSpec(**values)  # type: ignore[arg-type]


def _registry(
    tmp_path: Path,
    *,
    store: SQLiteStore | None = None,
    idempotency_key: str = "python-worker",
) -> tuple[ArtifactRegistry, str]:
    executable = Path(sys.executable).resolve(strict=True)
    registry_store = store or SQLiteStore(tmp_path / f"{idempotency_key}.db")
    registry_store.initialize()
    registry = ArtifactRegistry.from_store(
        registry_store,
        local_file_roots=(executable.parent,),
    )
    record = registry.register_file(
        workspace_id="training",
        idempotency_key=idempotency_key,
        path=executable,
        kind="training_worker_executable",
        display_name="Python test trainer",
    )
    return registry, record.artifact_id


def _worker(
    tmp_path: Path,
    code: str,
    *,
    store: SQLiteStore | None = None,
    idempotency_key: str = "python-worker",
    environment: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
    max_response_bytes: int = 64 * 1024,
) -> SubprocessTrainingWorker:
    registry, artifact_id = _registry(
        tmp_path,
        store=store,
        idempotency_key=idempotency_key,
    )
    return SubprocessTrainingWorker(
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        arguments=("-c", code),
        environment=environment,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


_SUCCESS_CODE = r"""
import json
import sys

request = json.loads(sys.stdin.buffer.read())
step = request["step_index"]
materials = request["materials"]
response = {
    "candidate_sha256": "b" * 64 if step == 1 else None,
    "completed": step == 1,
    "protocol_version": 2,
    "resume_state": {
        "epoch": step + 1,
        "material_count": len(materials["items"]),
        "material_digest": materials["training_material_sha256"],
        "manifest_digest": materials["package_manifest_sha256"],
    },
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip()


def test_real_subprocess_receives_exact_material_identity_and_resumes(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    spec = _spec(materials)
    worker = _worker(tmp_path, _SUCCESS_CODE)

    first = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    assert first.completed is False
    envelope = first.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    assert envelope["protocol_version"] == 2
    assert envelope["trainer_sha256"]
    state = envelope["trainer_state"]
    assert isinstance(state, dict)
    assert state["material_count"] == 2
    assert state["material_digest"] == materials.training_material_sha256
    assert state["manifest_digest"] == _package().manifest_sha256
    assert str(tmp_path) not in json.dumps(first.resume_state)

    second = worker.step(
        spec=spec,
        step_index=1,
        resume_state=first.resume_state,
        training_materials=materials,
    )
    assert second.completed is True
    assert second.candidate_sha256 == "b" * 64


def test_step_identity_is_stable_for_exact_replay(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    worker = _worker(tmp_path, _SUCCESS_CODE)
    spec = _spec(materials)

    first = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    replay = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    first_envelope = first.resume_state["_nika_subprocess"]
    replay_envelope = replay.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    assert isinstance(replay_envelope, dict)
    assert first_envelope["last_step_id"] == replay_envelope["last_step_id"]
    assert first_envelope["job_fingerprint"] == replay_envelope["job_fingerprint"]


def test_resume_rejects_different_registered_trainer_before_spawn(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    spec = _spec(materials)
    original = _worker(tmp_path, _SUCCESS_CODE, idempotency_key="trainer-a")
    first = original.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    marker = tmp_path / "replacement-started"
    replacement_code = f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip()
    replacement = _worker(
        tmp_path,
        replacement_code,
        idempotency_key="trainer-b",
    )
    with pytest.raises(TrainingWorkerError) as exc_info:
        replacement.step(
            spec=spec,
            step_index=1,
            resume_state=first.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert exc_info.value.code == "preflight_rejected"
    assert not marker.exists()


def test_tampered_resume_path_is_rejected_before_process_effect(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    spec = _spec(materials)
    marker = tmp_path / "step-one-started"
    code = f"""
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
    "resume_state": {{"epoch": request["step_index"] + 1}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip()
    worker = _worker(tmp_path, code)
    first = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    envelope = first.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    trainer_state["physical_path"] = str(tmp_path / "secret-material.bin")

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=spec,
            step_index=1,
            resume_state=first.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert exc_info.value.code == "preflight_rejected"
    assert not marker.exists()


def test_trainer_cannot_echo_physical_material_path_into_durable_state(
    tmp_path: Path,
) -> None:
    materials = _materials(tmp_path)
    path = str(materials.materials[0].path)
    code = r"""
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 2,
    "resume_state": {"physical_path": request["materials"]["items"][0]["path"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip()
    worker = _worker(tmp_path, code)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code == "invalid_result"
    assert path not in str(exc_info.value)


def test_parent_environment_is_not_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_TRAINING_SECRET", "must-not-leak")
    materials = _materials(tmp_path)
    code = r"""
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
""".strip()
    result = _worker(tmp_path, code).step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    state = envelope["trainer_state"]
    assert isinstance(state, dict)
    assert state["secret_seen"] is False


def test_explicit_environment_is_the_only_environment_exposed(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    code = r"""
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
""".strip()
    result = _worker(
        tmp_path,
        code,
        environment={"NIKA_ALLOWED": "yes"},
    ).step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    state = envelope["trainer_state"]
    assert isinstance(state, dict)
    assert state["allowed"] == "yes"


def test_nonzero_exit_is_unknown_effect_and_secret_minimized(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    code = r"""
import sys
sys.stdin.buffer.read()
sys.stderr.write("TOP-SECRET-TRAINING-DATA")
raise SystemExit(9)
""".strip()
    worker = _worker(tmp_path, code)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code == "process_failed"
    assert "TOP-SECRET-TRAINING-DATA" not in str(exc_info.value)
    assert "9" not in str(exc_info.value)


def test_timeout_after_spawn_is_unknown_effect(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    code = "import sys, time; sys.stdin.buffer.read(); time.sleep(10)"
    worker = _worker(tmp_path, code, timeout_seconds=0.1)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code == "timeout"


def test_oversized_response_after_spawn_is_unknown_effect(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    code = r"""
import sys
sys.stdin.buffer.read()
sys.stdout.buffer.write(b"x" * 4096)
sys.stdout.buffer.flush()
""".strip()
    worker = _worker(tmp_path, code, max_response_bytes=1024)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code == "response_too_large"


def test_wrong_protocol_and_step_identity_fail_as_unknown_effect(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    code = r"""
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": "0" * 64,
}
sys.stdout.write(json.dumps(response))
""".strip()
    worker = _worker(tmp_path, code)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code == "unsupported_protocol"


def test_changed_registered_executable_is_rejected_before_spawn(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    store = SQLiteStore(tmp_path / "tampered-registry.db")
    store.initialize()
    fake = tmp_path / "registered-trainer.bin"
    fake.write_bytes(b"original")
    registry = ArtifactRegistry.from_store(store, local_file_roots=(tmp_path,))
    record = registry.register_file(
        workspace_id="training",
        idempotency_key="trainer",
        path=fake,
        kind="training_worker_executable",
    )
    fake.write_bytes(b"mutated")
    worker = SubprocessTrainingWorker(
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
    )

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert exc_info.value.code == "trainer_artifact_unavailable"


def test_opaque_trainer_reference_is_never_executed(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    store = SQLiteStore(tmp_path / "opaque-registry.db")
    store.initialize()
    registry = ArtifactRegistry.from_store(store)
    record = registry.register_reference(
        workspace_id="training",
        idempotency_key="opaque-trainer",
        reference="artifact://trainer/opaque",
        sha256="c" * 64,
        size_bytes=10,
        kind="training_worker_executable",
    )
    worker = SubprocessTrainingWorker(
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
    )

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert exc_info.value.code == "trainer_artifact_unavailable"


def test_training_material_identity_mismatch_is_no_effect(tmp_path: Path) -> None:
    materials = _materials(tmp_path)
    worker = _worker(tmp_path, _SUCCESS_CODE)
    spec = _spec(materials, training_material_sha256="f" * 64)

    with pytest.raises(TrainingWorkerError) as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert exc_info.value.code == "training_material_identity_mismatch"


@dataclass
class _Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=5.0,
            memory_percent=10.0,
            available_memory_bytes=8_000_000_000,
        )


def _store_with_task(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("training-task", "training", "training-runtime", "pending", "{}", now, now),
        )
    return store


_RUNTIME_SUCCESS_CODE = r"""
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": "d" * 64,
    "completed": True,
    "protocol_version": 2,
    "resume_state": {"epoch": 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip()


def test_training_runtime_persists_completed_worker_evidence_without_paths(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "runtime.db")
    materials = _materials(tmp_path)
    worker = _worker(tmp_path, _RUNTIME_SUCCESS_CODE, store=store)
    runtime = TrainingRuntime(
        resources=ResourceManager(store, _Observer()),
        checkpoints=CheckpointService(store),
        training_materials=materials,
    )

    result = runtime.run(_spec(materials), worker)

    assert result.state is TrainingRunState.COMPLETED
    assert result.candidate_sha256 == "d" * 64
    assert result.training_material_sha256 == materials.training_material_sha256
    checkpoint = CheckpointService(store).latest("training-task")
    assert checkpoint is not None
    durable = json.dumps(checkpoint.payload, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in durable
    assert "_nika_subprocess" in durable


def test_runtime_maps_post_spawn_failure_to_reconcile_required(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "runtime-unknown.db")
    materials = _materials(tmp_path)
    worker = _worker(
        tmp_path,
        "import sys; sys.stdin.buffer.read(); raise SystemExit(7)",
        store=store,
    )
    runtime = TrainingRuntime(
        resources=ResourceManager(store, _Observer()),
        checkpoints=CheckpointService(store),
        training_materials=materials,
    )

    result = runtime.run(_spec(materials), worker)

    assert result.state is TrainingRunState.RECONCILE_REQUIRED
    assert result.reason == "worker_unknown:process_failed"


def test_runtime_maps_pre_spawn_artifact_failure_to_failed(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "runtime-no-effect.db")
    materials = _materials(tmp_path)
    fake = tmp_path / "runtime-trainer.bin"
    fake.write_bytes(b"original")
    registry = ArtifactRegistry.from_store(store, local_file_roots=(tmp_path,))
    record = registry.register_file(
        workspace_id="training",
        idempotency_key="runtime-fake",
        path=fake,
        kind="training_worker_executable",
    )
    fake.write_bytes(b"changed")
    worker = SubprocessTrainingWorker(
        artifact_registry=registry,
        trainer_artifact_id=record.artifact_id,
    )
    runtime = TrainingRuntime(
        resources=ResourceManager(store, _Observer()),
        checkpoints=CheckpointService(store),
        training_materials=materials,
    )

    result = runtime.run(_spec(materials), worker)

    assert result.state is TrainingRunState.FAILED
    assert result.reason == "worker_no_effect:trainer_artifact_unavailable"
