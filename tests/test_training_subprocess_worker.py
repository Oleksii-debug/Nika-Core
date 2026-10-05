from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import nika_core.training_adapters.subprocess_worker as subprocess_worker_module
from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.training_adapters import (
    SubprocessTrainingWorker,
    TrainingSubprocessError,
    training_runtime_registry_metadata,
)
from nika_core.training_materials import (
    ResolvedTrainingPackage,
    TrainingMaterialEvidence,
    resolve_training_materials,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingWorkerFailureEffect,
)

_WORKSPACE_ID = "subprocess-training"
_TRAINING_BODY = b'{"prompt":"train","response":"ok"}\n'
_VALIDATION_BODY = b'{"prompt":"validate","response":"ok"}\n'
_RUNTIME_VERSIONS = {
    "torch": "2.14.1",
    "transformers": "5.18.2",
    "peft": "0.21.2",
    "accelerate": "1.15.3",
    "gguf": "0.19.1",
    "safetensors": "0.8.2",
}


def _runtime_environment(
    versions: dict[str, str] | None = None,
) -> dict[str, str]:
    selected = dict(_RUNTIME_VERSIONS if versions is None else versions)
    encoded = json.dumps(
        selected,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(
        b"nika-peft-runtime-manifest-v1\x00" + encoded
    ).hexdigest()
    return {
        "NIKA_TRAINER_TORCH_VERSION": selected["torch"],
        "NIKA_TRAINER_TRANSFORMERS_VERSION": selected["transformers"],
        "NIKA_TRAINER_PEFT_VERSION": selected["peft"],
        "NIKA_TRAINER_ACCELERATE_VERSION": selected["accelerate"],
        "NIKA_TRAINER_GGUF_VERSION": selected["gguf"],
        "NIKA_TRAINER_SAFETENSORS_VERSION": selected["safetensors"],
        "NIKA_TRAINER_RUNTIME_MANIFEST_SHA256": manifest_sha256,
    }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "trainer.py"
    path.write_text(body, encoding="utf-8")
    return path


def _frozen_package() -> FrozenLearningPackage:
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


def _resolved_materials(
    tmp_path: Path,
    *,
    workspace_id: str = _WORKSPACE_ID,
) -> ResolvedTrainingPackage:
    store = ContentAddressedBlobStore(tmp_path / f"materials-{_sha256(workspace_id.encode())}")
    store.put_bytes(workspace_id, _TRAINING_BODY)
    store.put_bytes(workspace_id, _VALIDATION_BODY)
    return resolve_training_materials(
        _frozen_package(),
        workspace_id=workspace_id,
        blob_store=store,
    )


def _spec(
    materials: ResolvedTrainingPackage,
    *,
    job_id: str = "job-1",
    max_steps: int = 3,
) -> TrainingJobSpec:
    return TrainingJobSpec(
        job_id=job_id,
        task_id=f"task-{job_id}",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity(
            "models/base",
            materials.evidence.base_artifact_sha256,
        ),
        frozen_package_sha256=materials.evidence.package_manifest_sha256,
        training_material_sha256=materials.training_material_sha256,
        scale_authorization_sha256=_sha256(b"subprocess-scale-authorization"),
        candidate_artifact_ref=f"models/candidate/{job_id}",
        max_steps=max_steps,
    )


def _registry_for_python(
    tmp_path: Path,
    script: Path | None = None,
    *,
    trainer_metadata: dict[str, str] | None = None,
) -> tuple[ArtifactRegistry, str, Path, str | None]:
    executable = Path(sys.executable).resolve()
    roots = [executable.parent]
    if script is not None and script.parent.resolve() not in roots:
        roots.append(script.parent.resolve())
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "artifacts.sqlite3"),
        local_file_roots=tuple(roots),
    )
    record = registry.register_file(
        workspace_id="trainer-tests",
        idempotency_key="python-executable",
        path=executable,
        kind="training_executable",
        metadata={} if trainer_metadata is None else dict(trainer_metadata),
    )
    script_artifact_id: str | None = None
    if script is not None:
        script_record = registry.register_file(
            workspace_id="trainer-tests",
            idempotency_key="trainer-script",
            path=script,
            kind="training_command_file",
        )
        script_artifact_id = script_record.artifact_id
    return registry, record.artifact_id, executable, script_artifact_id


def _worker(
    tmp_path: Path,
    script: Path,
    *,
    trainer_metadata: dict[str, str] | None = None,
    **kwargs: object,
) -> tuple[SubprocessTrainingWorker, ArtifactRegistry, str]:
    registry, artifact_id, executable, script_artifact_id = _registry_for_python(
        tmp_path,
        script,
        trainer_metadata=trainer_metadata,
    )
    assert script_artifact_id is not None
    worker = SubprocessTrainingWorker(
        (str(executable), str(script)),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        command_artifact_ids={1: script_artifact_id},
        **kwargs,
    )
    return worker, registry, artifact_id


def test_real_subprocess_receives_exact_registry_and_material_identity(tmp_path: Path) -> None:
    observed_path = tmp_path / "observed.json"
    trainer = _script(
        tmp_path,
        f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
Path({str(observed_path)!r}).write_text(
    json.dumps(request, sort_keys=True),
    encoding="utf-8",
)
response = {{
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {{"epoch": 1}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    worker, registry, artifact_id = _worker(tmp_path, trainer)

    result = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    record = registry.get(artifact_id)
    assert observed["protocol_version"] == 3
    assert len(worker.execution_plan_sha256) == 64
    assert observed["trainer_artifact_id"] == artifact_id
    assert observed["trainer_sha256"] == record.sha256
    assert observed["command_sha256"] == observed["job"]["command_sha256"]
    assert {item["argument_index"] for item in observed["command_artifacts"]} == {0, 1}
    assert observed["job"]["frozen_package_sha256"] == spec.frozen_package_sha256
    assert observed["job"]["training_material_sha256"] == spec.training_material_sha256
    assert observed["training_materials"]["training_material_sha256"] == (
        spec.training_material_sha256
    )
    assert observed["training_materials"]["package_manifest_sha256"] == (
        spec.frozen_package_sha256
    )
    assert [item["path"] for item in observed["training_materials"]["materials"]] == [
        os.fspath(item.path) for item in materials.materials
    ]
    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    assert envelope["command_sha256"] == observed["command_sha256"]
    assert envelope["consumed_materials_sha256"] == (
        observed["training_materials"]["required_consumed_materials_sha256"]
    )
    assert envelope["trainer_artifact_id"] == artifact_id
    assert envelope["trainer_sha256"] == record.sha256


def test_two_step_resume_and_completion(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
step_index = request["step_index"]
response = {
    "candidate_sha256": "b" * 64 if step_index == 1 else None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": step_index == 1,
    "protocol_version": request["protocol_version"],
    "resume_state": {"next_epoch": step_index + 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    worker, _, _ = _worker(tmp_path, trainer)

    first = worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    second = worker.step(
        spec=spec,
        step_index=1,
        resume_state=first.resume_state,
        training_materials=materials,
    )

    assert first.completed is False
    assert second.completed is True
    assert second.candidate_sha256 == "b" * 64


def test_resume_rejects_tampered_material_attestation_before_process_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "second-step-started"
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
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "protocol_version": request["protocol_version"],
    "resume_state": {{"step": request["step_index"]}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    first = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    tampered = json.loads(json.dumps(first.resume_state))
    envelope = tampered["_nika_subprocess"]
    assert isinstance(envelope, dict)
    envelope["consumed_materials_sha256"] = "0" * 64

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=1,
            resume_state=tampered,
            training_materials=materials,
        )

    assert exc_info.value.code == "resume_state_material_attestation_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_resume_binds_trainer_artifact_id_even_when_digest_is_same(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"step": request["step_index"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    registry, first_id, executable, script_artifact_id = _registry_for_python(
        tmp_path,
        trainer,
    )
    assert script_artifact_id is not None
    first_worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=first_id,
        command_artifact_ids={1: script_artifact_id},
    )
    first = first_worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    second_record = registry.register_file(
        workspace_id="trainer-tests",
        idempotency_key="python-executable-alias",
        path=executable,
        kind="training_executable",
    )
    replacement = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=second_record.artifact_id,
        command_artifact_ids={1: script_artifact_id},
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        replacement.step(
            spec=spec,
            step_index=1,
            resume_state=first.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.code == "resume_state_command_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT


def test_resume_binds_frozen_and_material_identity(tmp_path: Path) -> None:
    marker = tmp_path / "second-started"
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
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {{"step": request["step_index"]}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    first_materials = _resolved_materials(tmp_path, workspace_id="workspace-a")
    second_materials = _resolved_materials(tmp_path, workspace_id="workspace-b")
    worker, _, _ = _worker(tmp_path, trainer)
    first = worker.step(
        spec=_spec(first_materials),
        step_index=0,
        resume_state={},
        training_materials=first_materials,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(second_materials),
            step_index=1,
            resume_state=first.resume_state,
            training_materials=second_materials,
        )

    assert exc_info.value.code == "resume_state_job_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["spec", "base"])
def test_mutated_job_spec_is_revalidated_before_process_effect(
    tmp_path: Path,
    mode: str,
) -> None:
    marker = tmp_path / "invalid-spec-started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials, max_steps=1)
    if mode == "spec":
        object.__setattr__(spec, "training_material_sha256", "invalid")
    else:
        object.__setattr__(spec.base_artifact, "sha256", "invalid")
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_spec_invalid"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_material_digest_mismatch_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = TrainingJobSpec(
        job_id="job-mismatch",
        task_id="task-mismatch",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity(
            "models/base",
            materials.evidence.base_artifact_sha256,
        ),
        frozen_package_sha256=materials.evidence.package_manifest_sha256,
        training_material_sha256="f" * 64,
        scale_authorization_sha256=_sha256(b"subprocess-scale-authorization"),
        candidate_artifact_ref="models/candidate/mismatch",
        max_steps=1,
    )
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_material_identity_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_materials_are_reverified_at_subprocess_effect_boundary(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials, max_steps=1)
    worker, _, _ = _worker(tmp_path, trainer)
    material_path = materials.materials[0].path
    material_path.write_bytes(b"x" * materials.materials[0].evidence.byte_count)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_material_verification_failed"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_material_tamper_during_spawn_is_unknown_and_process_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "material-spawn-effect"
    trainer = _script(
        tmp_path,
        f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
Path({str(marker)!r}).write_text("effect", encoding="utf-8")
response = {{
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {{}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    material_path = materials.materials[0].path
    byte_count = materials.materials[0].evidence.byte_count
    real_popen = subprocess.Popen

    def mutating_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        material_path.write_bytes(b"x" * byte_count)
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "nika_core.training_adapters.subprocess_worker.subprocess.Popen",
        mutating_popen,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_material_changed_after_process_start"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert not marker.exists()


def test_inner_material_evidence_cannot_diverge_from_frozen_package_before_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "trainer-started"
    trainer = _script(
        tmp_path,
        f"""
import hashlib
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
Path({str(marker)!r}).write_text("started", encoding="utf-8")
observations = []
for item in request["training_materials"]["materials"]:
    body = Path(item["path"]).read_bytes()
    observations.append({{
        "artifact_sha256": hashlib.sha256(body).hexdigest(),
        "byte_count": len(body),
        "split": item["split"],
    }})
encoded = json.dumps(
    observations,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
domain = b"nika-training-consumed-materials-v1" + bytes([0])
response = {{
    "candidate_sha256": None,
    "completed": False,
    "consumed_materials_sha256": hashlib.sha256(domain + encoded).hexdigest(),
    "protocol_version": request["protocol_version"],
    "resume_state": {{}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials, max_steps=1)
    material = materials.materials[0]
    original_evidence = material.evidence
    replacement = b"x" * original_evidence.byte_count
    material.path.write_bytes(replacement)
    object.__setattr__(
        material,
        "evidence",
        TrainingMaterialEvidence(
            split=original_evidence.split,
            artifact_sha256=_sha256(replacement),
            provenance_sha256=original_evidence.provenance_sha256,
            license_evidence_sha256=original_evidence.license_evidence_sha256,
            record_count=original_evidence.record_count,
            byte_count=original_evidence.byte_count,
        ),
    )
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_materials_invalid"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_consumed_material_attestation_mismatch_is_unknown_effect(
    tmp_path: Path,
) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "consumed_materials_sha256": "0" * 64,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_material_attestation_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_material_swap_after_final_parent_reverify_is_detected_by_trainer_attestation(
    tmp_path: Path,
) -> None:
    request_seen = tmp_path / "request-seen"
    trainer = _script(
        tmp_path,
        f"""
import hashlib
import json
import sys
import time
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
Path({str(request_seen)!r}).write_text("seen", encoding="utf-8")
time.sleep(0.2)
observations = []
for item in request["training_materials"]["materials"]:
    body = Path(item["path"]).read_bytes()
    observations.append({{
        "artifact_sha256": hashlib.sha256(body).hexdigest(),
        "byte_count": len(body),
        "split": item["split"],
    }})
encoded = json.dumps(
    observations,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
domain = b"nika-training-consumed-materials-v1" + bytes([0])
response = {{
    "candidate_sha256": None,
    "completed": False,
    "consumed_materials_sha256": hashlib.sha256(domain + encoded).hexdigest(),
    "protocol_version": request["protocol_version"],
    "resume_state": {{}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    target = materials.materials[0]
    replacement = b"x" * target.evidence.byte_count

    def replace_after_request() -> None:
        deadline = time.monotonic() + 3.0
        while not request_seen.exists():
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)
        target.path.write_bytes(replacement)

    mutator = threading.Thread(target=replace_after_request, daemon=True)
    mutator.start()
    try:
        with pytest.raises(TrainingSubprocessError) as exc_info:
            worker.step(
                spec=_spec(materials, max_steps=1),
                step_index=0,
                resume_state={},
                training_materials=materials,
            )
    finally:
        mutator.join(timeout=1.0)

    assert not mutator.is_alive()
    assert request_seen.exists()
    assert exc_info.value.code == "training_subprocess_material_attestation_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_registry_artifact_must_match_command_executable(tmp_path: Path) -> None:
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials, max_steps=1)
    registry, artifact_id, _, _ = _registry_for_python(tmp_path)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        SubprocessTrainingWorker(
            (str(tmp_path / "different.exe"),),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        ).step(
            spec=spec,
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "command_artifact_argument_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT


def test_absolute_command_file_requires_registry_binding(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)

    with pytest.raises(ValueError, match="absolute command file arguments"):
        SubprocessTrainingWorker(
            (str(executable), str(trainer)),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        )


def test_relative_command_file_argument_is_rejected(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)

    with pytest.raises(ValueError, match="unbound command arguments"):
        SubprocessTrainingWorker(
            (str(executable), trainer.name),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        )


def test_bound_command_file_tamper_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    trainer.write_text("raise SystemExit(0)", encoding="utf-8")

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "command_artifact_not_verified"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_command_tamper_during_spawn_is_unknown_and_process_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "spawn-race-effect"
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    real_popen = subprocess.Popen

    def mutating_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        trainer.write_text(
            f"""
import time
from pathlib import Path

time.sleep(2)
Path({str(marker)!r}).write_text("effect", encoding="utf-8")
""".strip(),
            encoding="utf-8",
        )
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "nika_core.training_adapters.subprocess_worker.subprocess.Popen",
        mutating_popen,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "command_artifact_changed_after_process_start"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert not marker.exists()


def test_post_spawn_verification_consumes_process_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "deadline-effect"
    trainer = _script(
        tmp_path,
        f"""
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
Path({str(marker)!r}).write_text("effect", encoding="utf-8")
response = {{
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {{}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer, timeout_seconds=0.05)
    original_verify = worker._verify_command_artifacts
    calls = 0

    def delayed_verify(expected_records: object) -> None:
        nonlocal calls
        calls += 1
        original_verify(expected_records)  # type: ignore[arg-type]
        if calls == 2:
            time.sleep(0.08)

    monkeypatch.setattr(worker, "_verify_command_artifacts", delayed_verify)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert calls == 2
    assert exc_info.value.code == "training_subprocess_timeout"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
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
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"secret_seen": os.getenv("NIKA_TRAINING_SECRET") is not None},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    result = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["secret_seen"] is False


def test_nonzero_exit_is_unknown_effect_and_stderr_is_minimized(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys
sys.stderr.write("TOP-SECRET-TRAINING-DATA")
raise SystemExit(9)
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_nonzero_exit"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert "TOP-SECRET-TRAINING-DATA" not in str(exc_info.value)


def test_timeout_is_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import time
time.sleep(10)
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer, timeout_seconds=0.1)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_timeout"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_timeout_kills_trainer_descendant_process_tree(tmp_path: Path) -> None:
    spawned = tmp_path / "descendant-spawned.txt"
    survived = tmp_path / "descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.0); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    trainer = _script(
        tmp_path,
        f"""
import pathlib
import subprocess
import sys
import time

time.sleep(0.2)
subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(survived)!r}])
pathlib.Path({str(spawned)!r}).write_text("spawned", encoding="utf-8")
sys.stdin.buffer.read()
time.sleep(30)
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer, timeout_seconds=1.5)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_timeout"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert spawned.exists(), "test did not prove that a trainer descendant was started"
    time.sleep(1.3)
    assert not survived.exists(), "trainer descendant escaped timeout containment"


def test_success_does_not_leave_trainer_descendant_running(tmp_path: Path) -> None:
    spawned = tmp_path / "success-descendant-spawned.txt"
    survived = tmp_path / "success-descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.0); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    trainer = _script(
        tmp_path,
        f"""
import json
import pathlib
import subprocess
import sys
import time

time.sleep(0.2)
subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(survived)!r}])
pathlib.Path({str(spawned)!r}).write_text("spawned", encoding="utf-8")
request = json.loads(sys.stdin.buffer.read())
response = {{
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {{"epoch": 1}},
    "step_id": request["step_id"],
}}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer, timeout_seconds=5)

    result = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    assert result.completed is False
    assert spawned.exists(), "test did not prove that a trainer descendant was started"
    time.sleep(1.3)
    assert not survived.exists(), "trainer descendant escaped successful-step containment"


def test_success_fails_closed_when_tree_cleanup_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)
    monkeypatch.setattr(
        "nika_core.training_adapters.subprocess_worker.terminate_process_tree",
        lambda process, job: False,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_containment_cleanup_failed"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_oversized_response_is_bounded_and_unknown_effect(tmp_path: Path) -> None:
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
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(
        tmp_path,
        trainer,
        max_response_bytes=1024,
        timeout_seconds=5,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_response_too_large"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_invalid_response_is_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys
sys.stdin.buffer.read()
sys.stdout.write("{not-json")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_invalid_json_response"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_command_must_not_be_a_shell_string(tmp_path: Path) -> None:
    registry, artifact_id, _, _ = _registry_for_python(tmp_path)
    with pytest.raises(TypeError, match="not a shell string"):
        SubprocessTrainingWorker(
            "python trainer.py",
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        )


def test_trainer_artifact_id_must_be_exact_sha256(tmp_path: Path) -> None:
    registry, _, executable, _ = _registry_for_python(tmp_path)
    with pytest.raises(ValueError, match="trainer_artifact_id"):
        SubprocessTrainingWorker(
            (str(executable),),
            artifact_registry=registry,
            trainer_artifact_id="not-a-digest",
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, 0.0, True])
def test_invalid_timeout_is_rejected(tmp_path: Path, timeout: object) -> None:
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker(
            (str(executable),),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_huge_integer_timeout_is_rejected_without_overflow(tmp_path: Path) -> None:
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker(
            (str(executable),),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
            timeout_seconds=10**10000,  # type: ignore[arg-type]
        )


def test_deeply_nested_response_stays_typed_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys

sys.stdin.buffer.read()
sys.stdout.write("[" * 5000 + "0" + "]" * 5000)
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN
    assert exc_info.value.code in {
        "training_subprocess_invalid_json_response",
        "training_subprocess_response_not_object",
    }


def test_step_identity_binds_registry_artifact_id_with_same_trainer_digest(
    tmp_path: Path,
) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"observed_step_id": request["step_id"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    registry, first_id, executable = _registry_for_python(tmp_path)
    second = registry.register_file(
        workspace_id="trainer-tests",
        idempotency_key="python-executable-second-authority",
        path=executable,
        kind="training_executable",
    )

    first_worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=first_id,
    )
    second_worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=second.artifact_id,
    )

    first = first_worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )
    second_result = second_worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    first_envelope = first.resume_state["_nika_subprocess"]
    second_envelope = second_result.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    assert isinstance(second_envelope, dict)
    first_state = first_envelope["trainer_state"]
    second_state = second_envelope["trainer_state"]
    assert isinstance(first_state, dict)
    assert isinstance(second_state, dict)
    assert first_envelope["trainer_sha256"] == second_envelope["trainer_sha256"]
    assert first_state["observed_step_id"] != second_state["observed_step_id"]


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
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"allowed": os.getenv("NIKA_ALLOWED")},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(
        tmp_path,
        trainer,
        environment={"NIKA_ALLOWED": "yes"},
    )

    result = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["allowed"] == "yes"


def test_runtime_environment_requires_registry_authority(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    with pytest.raises(ValueError, match="Registry-authoritative trainer metadata"):
        _worker(
            tmp_path,
            trainer,
            environment=_runtime_environment(),
        )


def test_registry_runtime_identity_is_injected_into_child_process(tmp_path: Path) -> None:
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
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "protocol_version": request["protocol_version"],
    "resume_state": {
        "torch": os.getenv("NIKA_TRAINER_TORCH_VERSION"),
        "transformers": os.getenv("NIKA_TRAINER_TRANSFORMERS_VERSION"),
        "manifest": os.getenv("NIKA_TRAINER_RUNTIME_MANIFEST_SHA256"),
        "deployment": os.getenv("NIKA_TRAINER_DEPLOYMENT_ARTIFACT_ID"),
        "deployment_sha256": os.getenv("NIKA_TRAINER_DEPLOYMENT_SHA256"),
        "allowed": os.getenv("NIKA_ALLOWED"),
    },
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    worker, registry, artifact_id = _worker(
        tmp_path,
        trainer,
        trainer_metadata=metadata,
        environment={"NIKA_ALLOWED": "yes"},
    )
    materials = _resolved_materials(tmp_path)

    result = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["torch"] == _RUNTIME_VERSIONS["torch"]
    assert trainer_state["transformers"] == _RUNTIME_VERSIONS["transformers"]
    assert trainer_state["manifest"] == _runtime_environment()[
        "NIKA_TRAINER_RUNTIME_MANIFEST_SHA256"
    ]
    assert trainer_state["deployment"] == artifact_id
    assert trainer_state["deployment_sha256"] == registry.get(artifact_id).sha256
    assert trainer_state["allowed"] == "yes"


def test_runtime_deployment_artifact_id_must_match_registry_authority(
    tmp_path: Path,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    environment = _runtime_environment()
    environment["NIKA_TRAINER_DEPLOYMENT_ARTIFACT_ID"] = "0" * 64

    with pytest.raises(ValueError, match="deployment artifact identity does not match"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
            environment=environment,
        )


def test_runtime_deployment_sha256_must_match_registry_authority(
    tmp_path: Path,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    environment = _runtime_environment()
    environment["NIKA_TRAINER_DEPLOYMENT_SHA256"] = "0" * 64

    with pytest.raises(ValueError, match="deployment digest does not match"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
            environment=environment,
        )


def test_runtime_environment_must_match_registry_authority(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    drifted = dict(_RUNTIME_VERSIONS)
    drifted["transformers"] = "5.18.3"

    with pytest.raises(ValueError, match="does not match Registry-authoritative"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
            environment=_runtime_environment(drifted),
        )


def test_runtime_registry_metadata_must_be_complete(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    del metadata["nika.training.runtime.torch.version"]

    with pytest.raises(ValueError, match="incomplete or ambiguous"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
        )


def test_runtime_environment_manifest_digest_must_match_versions(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    environment = _runtime_environment()
    environment["NIKA_TRAINER_RUNTIME_MANIFEST_SHA256"] = "0" * 64

    with pytest.raises(ValueError, match="environment manifest digest is inconsistent"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
            environment=environment,
        )



@pytest.mark.skipif(os.name != "nt", reason="requires Windows file-sharing semantics")
def test_windows_launch_guard_blocks_replace_at_popen_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"guarded": True},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    replacement = tmp_path / "replacement.py"
    replacement.write_text("raise SystemExit(97)\n", encoding="utf-8")
    worker, _, _ = _worker(tmp_path, trainer)
    materials = _resolved_materials(tmp_path)
    real_popen = subprocess.Popen
    attempts = 0

    def racing_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal attempts
        attempts += 1
        try:
            os.replace(replacement, trainer)
        except OSError as exc:
            assert getattr(exc, "winerror", None) in {5, 32, 33}
        else:
            raise AssertionError(
                "Registry-bound command artifact was replaceable at process launch"
            )
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(
        subprocess_worker_module.subprocess,
        "Popen",
        racing_popen,
    )

    result = worker.step(
        spec=_spec(materials),
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    assert attempts == 1
    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["guarded"] is True

    # The guard is scoped to the child lifetime rather than permanently locking
    # Registry-owned files.
    os.replace(replacement, trainer)
    assert trainer.read_text(encoding="utf-8") == "raise SystemExit(97)\n"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file-sharing semantics")
def test_windows_launch_guard_reverifies_swap_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "unexpected-start"
    trainer = _script(tmp_path, "raise SystemExit(0)")
    replacement = tmp_path / "replacement.py"
    replacement.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    worker, _, _ = _worker(tmp_path, trainer)
    materials = _resolved_materials(tmp_path)
    real_verify = worker._verify_command_artifacts
    verify_calls = 0

    def verify_then_swap(records: object) -> None:
        nonlocal verify_calls
        verify_calls += 1
        real_verify(records)  # type: ignore[arg-type]
        if verify_calls == 1:
            os.replace(replacement, trainer)

    def process_must_not_start(*args: object, **kwargs: object) -> object:
        raise AssertionError("process effect reached after command artifact replacement")

    monkeypatch.setattr(worker, "_verify_command_artifacts", verify_then_swap)
    monkeypatch.setattr(
        subprocess_worker_module.subprocess,
        "Popen",
        process_must_not_start,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert verify_calls == 2
    assert exc_info.value.code == "command_artifact_not_verified"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_runtime_registry_metadata_drift_fails_before_process_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    worker, registry, artifact_id = _worker(
        tmp_path,
        trainer,
        trainer_metadata=metadata,
        environment=_runtime_environment(),
    )
    real_get = registry.get
    original = real_get(artifact_id)
    drifted_versions = dict(_RUNTIME_VERSIONS)
    drifted_versions["torch"] = "2.14.2"
    drifted_record = original.model_copy(
        update={"metadata": training_runtime_registry_metadata(drifted_versions)}
    )

    def changed_get(requested_artifact_id: str) -> object:
        if requested_artifact_id == artifact_id:
            return drifted_record
        return real_get(requested_artifact_id)

    monkeypatch.setattr(registry, "get", changed_get)
    materials = _resolved_materials(tmp_path)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_execution_plan_changed"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_runtime_registry_trainer_digest_drift_fails_before_process_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    worker, registry, artifact_id = _worker(
        tmp_path,
        trainer,
        trainer_metadata=metadata,
    )
    real_get = registry.get
    original = real_get(artifact_id)
    drifted_record = original.model_copy(update={"sha256": "0" * 64})

    def changed_get(requested_artifact_id: str) -> object:
        if requested_artifact_id == artifact_id:
            return drifted_record
        return real_get(requested_artifact_id)

    monkeypatch.setattr(registry, "get", changed_get)
    materials = _resolved_materials(tmp_path)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_execution_plan_changed"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_training_runtime_registry_metadata_rejects_invalid_versions() -> None:
    missing = dict(_RUNTIME_VERSIONS)
    del missing["gguf"]
    with pytest.raises(ValueError, match="exact deployment set"):
        training_runtime_registry_metadata(missing)

    malformed = dict(_RUNTIME_VERSIONS)
    malformed["torch"] = " 2.14.1"
    with pytest.raises(ValueError, match="canonical text"):
        training_runtime_registry_metadata(malformed)


def test_registry_runtime_injection_respects_effective_environment_limit(
    tmp_path: Path,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")
    metadata = training_runtime_registry_metadata(_RUNTIME_VERSIONS)
    environment = {f"NIKA_FIELD_{index}": "value" for index in range(122)}

    with pytest.raises(ValueError, match="too many entries"):
        _worker(
            tmp_path,
            trainer,
            trainer_metadata=metadata,
            environment=environment,
        )


def test_resume_identity_binds_runtime_execution_limits(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"step": request["step_index"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    registry, artifact_id, executable, script_artifact_id = _registry_for_python(
        tmp_path,
        trainer,
    )
    assert script_artifact_id is not None
    first = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        command_artifact_ids={1: script_artifact_id},
        timeout_seconds=1.0,
    )
    replacement = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        command_artifact_ids={1: script_artifact_id},
        timeout_seconds=2.0,
    )
    assert first.execution_plan_sha256 != replacement.execution_plan_sha256

    result = first.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        replacement.step(
            spec=spec,
            step_index=1,
            resume_state=result.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.code == "resume_state_job_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT


def test_resume_identity_binds_explicit_environment(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {"step": request["step_index"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    spec = _spec(materials)
    registry, artifact_id, executable, script_artifact_id = _registry_for_python(
        tmp_path,
        trainer,
    )
    assert script_artifact_id is not None
    first_worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        command_artifact_ids={1: script_artifact_id},
        environment={"NIKA_TRAINING_MODE": "first"},
    )
    replacement_worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
        command_artifact_ids={1: script_artifact_id},
        environment={"NIKA_TRAINING_MODE": "second"},
    )

    first = first_worker.step(
        spec=spec,
        step_index=0,
        resume_state={},
        training_materials=materials,
    )

    with pytest.raises(TrainingSubprocessError) as exc_info:
        replacement_worker.step(
            spec=spec,
            step_index=1,
            resume_state=first.resume_state,
            training_materials=materials,
        )

    assert exc_info.value.code == "resume_state_command_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT


@pytest.mark.parametrize(
    "key",
    [
        "PYTHONPATH",
        "pythonhome",
        "LD_PRELOAD",
        "dyld_insert_libraries",
        "PATH",
        "Node_Options",
        "DOTNET_STARTUP_HOOKS",
    ],
)
def test_environment_rejects_runtime_loader_authority(
    tmp_path: Path,
    key: str,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    with pytest.raises(ValueError, match="runtime or loader authority"):
        _worker(tmp_path, trainer, environment={key: "untrusted"})


@pytest.mark.parametrize(
    "key",
    ["NIKA_API_KEY", "ACCESS_TOKEN", "DB_PASSWORD", "SERVICE_CREDENTIAL"],
)
def test_environment_rejects_credential_named_fields(
    tmp_path: Path,
    key: str,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    with pytest.raises(ValueError, match="credential material"):
        _worker(tmp_path, trainer, environment={key: "sensitive"})


def test_wrong_step_identity_is_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": "0" * 64,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_wrong_step_identity"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_unknown_response_field_is_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": False,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": request["step_id"],
    "unexpected": True,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_response_unexpected_fields"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_invalid_candidate_digest_is_unknown_effect(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": "not-a-digest",
    "consumed_materials_sha256": (
        request["training_materials"]["required_consumed_materials_sha256"]
    ),
    "completed": True,
    "protocol_version": request["protocol_version"],
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_subprocess_invalid_result_evidence"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.UNKNOWN


def test_invalid_resume_state_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    materials = _resolved_materials(tmp_path)
    worker, _, _ = _worker(tmp_path, trainer)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials),
            step_index=1,
            resume_state={"bad": object()},
            training_materials=materials,
        )

    assert exc_info.value.code == "training_resume_state_non_json_value"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()


def test_training_executable_must_be_absolute(tmp_path: Path) -> None:
    registry, artifact_id, _ = _registry_for_python(tmp_path)
    with pytest.raises(ValueError, match="absolute path"):
        SubprocessTrainingWorker(
            ("python",),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        )


def test_caller_supplied_trainer_digest_is_not_adapter_api(tmp_path: Path) -> None:
    registry, artifact_id, executable = _registry_for_python(tmp_path)
    with pytest.raises(TypeError):
        SubprocessTrainingWorker(
            (str(executable),),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
            trainer_sha256="c" * 64,  # type: ignore[call-arg]
        )


def test_command_option_values_are_rejected_from_process_argv(tmp_path: Path) -> None:
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)

    with pytest.raises(ValueError, match="simple option switches"):
        SubprocessTrainingWorker(
            (str(executable), "--token=secret"),
            artifact_registry=registry,
            trainer_artifact_id=artifact_id,
        )


def test_simple_option_switch_is_admitted(tmp_path: Path) -> None:
    registry, artifact_id, executable, _ = _registry_for_python(tmp_path)

    worker = SubprocessTrainingWorker(
        (str(executable), "-u"),
        artifact_registry=registry,
        trainer_artifact_id=artifact_id,
    )

    assert isinstance(worker, SubprocessTrainingWorker)


def test_command_artifact_kind_mismatch_fails_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    executable = Path(sys.executable).resolve()
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "wrong-kind.sqlite3"),
        local_file_roots=(executable.parent, tmp_path),
    )
    executable_record = registry.register_file(
        workspace_id="trainer-tests",
        idempotency_key="python-executable",
        path=executable,
        kind="training_executable",
    )
    script_record = registry.register_file(
        workspace_id="trainer-tests",
        idempotency_key="trainer-script",
        path=trainer,
        kind="dataset",
    )
    worker = SubprocessTrainingWorker(
        (str(executable), str(trainer)),
        artifact_registry=registry,
        trainer_artifact_id=executable_record.artifact_id,
        command_artifact_ids={1: script_record.artifact_id},
    )
    materials = _resolved_materials(tmp_path)

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(
            spec=_spec(materials, max_steps=1),
            step_index=0,
            resume_state={},
            training_materials=materials,
        )

    assert exc_info.value.code == "command_artifact_kind_mismatch"
    assert exc_info.value.effect is TrainingWorkerFailureEffect.NO_EFFECT
    assert not marker.exists()



@pytest.mark.parametrize(
    "key",
    ["NIKA_AUTHOR_MODE", "TOKENIZER_MODE", "AUTHORITY_MODE"],
)
def test_environment_allows_noncredential_substring_names(
    tmp_path: Path,
    key: str,
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    worker, _, _ = _worker(tmp_path, trainer, environment={key: "enabled"})

    assert worker is not None


def test_environment_rejects_case_insensitive_duplicate_keys(tmp_path: Path) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    with pytest.raises(ValueError, match="unique ignoring case"):
        _worker(
            tmp_path,
            trainer,
            environment={"NIKA_MODE": "one", "nika_mode": "two"},
        )


@pytest.mark.parametrize(
    "environment",
    [
        {"NIKA_\ud800": "value"},
        {"NIKA_MODE": "\ud800"},
        {"NIKA\nMODE": "value"},
        {"NIKA_MODE": "line\nbreak"},
    ],
)
def test_environment_rejects_noncanonical_text(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    trainer = _script(tmp_path, "raise SystemExit(0)")

    with pytest.raises(ValueError):
        _worker(tmp_path, trainer, environment=environment)


def test_command_rejects_noncanonical_text_before_registry_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        SubprocessTrainingWorker(
            (str(tmp_path / "trainer"), "\ud800"),
            artifact_registry=object(),  # type: ignore[arg-type]
            trainer_artifact_id="0" * 64,
        )
