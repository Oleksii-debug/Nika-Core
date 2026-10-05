from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "loop_c_peft_trainer.py"
_SPEC = importlib.util.spec_from_file_location("loop_c_peft_trainer", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
trainer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = trainer
_SPEC.loader.exec_module(trainer)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _config(tmp_path: Path) -> trainer.WorkerConfig:
    root = tmp_path / "base-model"
    root.mkdir()
    (root / "config.json").write_text('{"model_type":"test"}\n', encoding="utf-8")
    (root / "tokenizer.json").write_text('{"version":"1"}\n', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"model-bytes")
    return trainer.parse_config(
        {
            "schema_version": 1,
            "base_model_root": str(root),
            "output_root": str(tmp_path / "output"),
            "torch_version": "2.7.0",
            "transformers_version": "5.17.0",
            "peft_version": "0.21.0",
            "device": "cpu",
            "seed": 17,
            "torch_num_threads": 1,
            "max_sequence_length": 128,
            "micro_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "learning_rate": "0.0002",
            "weight_decay": "0",
            "lora_rank": 4,
            "lora_alpha": 8,
            "lora_dropout": "0.05",
            "target_modules": ["q_proj", "v_proj"],
            "prompt_separator": "\n",
        }
    )


def _materials(tmp_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    training = tmp_path / "training.jsonl"
    validation = tmp_path / "validation.jsonl"
    training_body = (
        b'{"prompt":"alpha","response":"one"}\n'
        b'{"prompt":"beta","response":"two"}\n'
    )
    validation_body = b'{"prompt":"hold","response":"out"}\n'
    training.write_bytes(training_body)
    validation.write_bytes(validation_body)
    entries = [
        {
            "artifact_sha256": _sha256(training_body),
            "byte_count": len(training_body),
            "path": str(training),
            "split": "training",
        },
        {
            "artifact_sha256": _sha256(validation_body),
            "byte_count": len(validation_body),
            "path": str(validation),
            "split": "validation",
        },
    ]
    observations = [
        {
            "artifact_sha256": item["artifact_sha256"],
            "byte_count": item["byte_count"],
            "split": item["split"],
        }
        for item in entries
    ]
    consumed = hashlib.sha256(
        trainer._MATERIAL_ATTESTATION_DOMAIN
        + trainer._canonical_json_bytes(observations)
    ).hexdigest()
    return (
        {
            "base_artifact_sha256": "",
            "package_manifest_sha256": "2" * 64,
            "required_consumed_materials_sha256": consumed,
            "training_material_sha256": "3" * 64,
            "materials": entries,
        },
        entries,
    )


def _request(
    tmp_path: Path,
    config: trainer.WorkerConfig,
    *,
    step_index: int = 0,
    max_steps: int = 2,
    resume_state: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    manifest = trainer.build_base_manifest(config.base_model_root)
    material_set, _ = _materials(tmp_path)
    material_set["base_artifact_sha256"] = manifest["manifest_sha256"]
    step_id = hashlib.sha256(f"step:{step_index}".encode()).hexdigest()
    request = {
        "command_artifacts": [
            {"argument_index": 0, "artifact_id": "a" * 64, "sha256": "b" * 64}
        ],
        "command_sha256": "c" * 64,
        "job": {
            "base_artifact": {
                "artifact_ref": "models/pilot-base",
                "sha256": manifest["manifest_sha256"],
            },
            "candidate_artifact_ref": "models/pilot-candidate",
            "command_sha256": "c" * 64,
            "frozen_package_sha256": "2" * 64,
            "job_id": "job-pilot",
            "max_steps": max_steps,
            "owner_id": "owner",
            "project_id": "project",
            "resource_scope": "model_training",
            "scale_authorization_sha256": "4" * 64,
            "task_id": "task-pilot",
            "training_material_sha256": "3" * 64,
        },
        "job_fingerprint": "5" * 64,
        "previous_step_id": None if step_index == 0 else hashlib.sha256(
            f"step:{step_index - 1}".encode()
        ).hexdigest(),
        "protocol_version": 3,
        "resume_state": {} if resume_state is None else resume_state,
        "step_id": step_id,
        "step_index": step_index,
        "trainer_artifact_id": "a" * 64,
        "trainer_sha256": "b" * 64,
        "training_materials": material_set,
    }
    return request, manifest


def _fake_train_step(
    *,
    request: dict[str, object],
    config: trainer.WorkerConfig,
    training: list[trainer.Example],
    checkpoint: Path | None,
    next_record_index: int,
    temporary_checkpoint: Path,
) -> tuple[int, str]:
    assert config.device == "cpu"
    step_index = int(request["step_index"])
    if step_index == 0:
        assert checkpoint is None
        assert next_record_index == 0
    else:
        assert checkpoint is not None
        assert checkpoint.name == f"checkpoint-{step_index:08d}"
    adapter = temporary_checkpoint / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "models/pilot-base",
                "peft_type": "LORA",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (adapter / "adapter_model.safetensors").write_bytes(
        f"adapter-step-{step_index}".encode()
    )
    (temporary_checkpoint / "optimizer.pt").write_bytes(
        f"optimizer-step-{step_index}".encode()
    )
    cursor = (next_record_index + 1) % len(training)
    return cursor, trainer.tree_sha256(adapter)


def test_strict_json_rejects_duplicate_keys_and_nan() -> None:
    with pytest.raises(trainer.WorkerInputError):
        trainer._load_json_bytes(
            b'{"a":1,"a":2}',
            max_bytes=1024,
            label="test",
        )
    with pytest.raises(trainer.WorkerInputError):
        trainer._load_json_bytes(
            b'{"a":NaN}',
            max_bytes=1024,
            label="test",
        )


def test_config_is_exact_cpu_only_and_uses_decimal_text(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.device == "cpu"
    assert str(config.learning_rate) == "0.0002"
    raw = {
        "schema_version": 1,
        "base_model_root": str(config.base_model_root),
        "output_root": str(config.output_root),
        "torch_version": config.torch_version,
        "transformers_version": config.transformers_version,
        "peft_version": config.peft_version,
        "device": "cuda",
        "seed": config.seed,
        "torch_num_threads": config.torch_num_threads,
        "max_sequence_length": config.max_sequence_length,
        "micro_batch_size": config.micro_batch_size,
        "gradient_accumulation_steps": config.gradient_accumulation_steps,
        "learning_rate": "2e-4",
        "weight_decay": "0",
        "lora_rank": config.lora_rank,
        "lora_alpha": config.lora_alpha,
        "lora_dropout": "0.05",
        "target_modules": list(config.target_modules),
        "prompt_separator": config.prompt_separator,
    }
    with pytest.raises(trainer.WorkerInputError, match="CPU"):
        trainer.parse_config(raw)
    raw["device"] = "cpu"
    with pytest.raises(trainer.WorkerInputError, match="canonical plain decimal"):
        trainer.parse_config(raw)


def test_base_manifest_binds_model_and_tokenizer_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    envelope = trainer.build_base_manifest(config.base_model_root)
    digest = trainer.verify_base_manifest(
        root=config.base_model_root,
        envelope=envelope,
        expected_sha256=envelope["manifest_sha256"],
    )
    assert digest == envelope["manifest_sha256"]

    (config.base_model_root / "tokenizer.json").write_text(
        '{"version":"mutated"}\n',
        encoding="utf-8",
    )
    with pytest.raises(trainer.WorkerInputError):
        trainer.verify_base_manifest(
            root=config.base_model_root,
            envelope=envelope,
            expected_sha256=envelope["manifest_sha256"],
        )


def test_base_manifest_rejects_unlisted_model_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    envelope = trainer.build_base_manifest(config.base_model_root)
    (config.base_model_root / "unexpected.bin").write_bytes(b"surprise")
    with pytest.raises(trainer.WorkerInputError, match="contents"):
        trainer.verify_base_manifest(
            root=config.base_model_root,
            envelope=envelope,
            expected_sha256=envelope["manifest_sha256"],
        )


def test_consumed_material_attestation_comes_from_verified_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request, _ = _request(tmp_path, config)
    materials = request["training_materials"]
    assert isinstance(materials, dict)
    training, validation, consumed = trainer.consume_training_materials(materials)
    assert [item.prompt for item in training] == ["alpha", "beta"]
    assert [item.response for item in validation] == ["out"]
    assert consumed == materials["required_consumed_materials_sha256"]

    training_path = Path(str(materials["materials"][0]["path"]))
    training_path.write_bytes(b'{"prompt":"changed","response":"bytes"}\n')
    with pytest.raises(trainer.WorkerInputError):
        trainer.consume_training_materials(materials)


def test_material_schema_is_exact_prompt_response_jsonl(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request, _ = _request(tmp_path, config)
    materials = request["training_materials"]
    assert isinstance(materials, dict)
    entry = materials["materials"][0]
    assert isinstance(entry, dict)
    body = b'{"prompt":"a","response":"b","metadata":"not-authorized"}\n'
    path = Path(str(entry["path"]))
    path.write_bytes(body)
    entry["artifact_sha256"] = _sha256(body)
    entry["byte_count"] = len(body)
    observations = [
        {
            "artifact_sha256": item["artifact_sha256"],
            "byte_count": item["byte_count"],
            "split": item["split"],
        }
        for item in materials["materials"]
    ]
    materials["required_consumed_materials_sha256"] = hashlib.sha256(
        trainer._MATERIAL_ATTESTATION_DOMAIN
        + trainer._canonical_json_bytes(observations)
    ).hexdigest()
    with pytest.raises(trainer.WorkerInputError, match="fields"):
        trainer.consume_training_materials(materials)


def test_two_step_protocol_checkpoint_resume_and_candidate_handoff(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_request, manifest = _request(tmp_path, config, step_index=0, max_steps=2)
    first = trainer.execute_request(
        first_request,
        config=config,
        base_manifest=manifest,
        train_step=_fake_train_step,
    )
    assert first["completed"] is False
    assert first["candidate_sha256"] is None
    assert first["protocol_version"] == 3
    first_resume = first["resume_state"]
    assert isinstance(first_resume, dict)
    assert first_resume["completed_steps"] == 1

    second_request, second_manifest = _request(
        tmp_path,
        config,
        step_index=1,
        max_steps=2,
        resume_state=first_resume,
    )
    second = trainer.execute_request(
        second_request,
        config=config,
        base_manifest=second_manifest,
        train_step=_fake_train_step,
    )
    assert second["completed"] is True
    assert isinstance(second["candidate_sha256"], str)
    assert len(second["candidate_sha256"]) == 64
    assert second["resume_state"]["completed_steps"] == 2

    handoff_path = config.output_root / ("5" * 64) / "candidate.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["artifact_ref"] == "models/pilot-candidate"
    assert handoff["sha256"] == second["candidate_sha256"]
    assert handoff["relative_path"] == "checkpoint-00000002/adapter"


def test_resume_rejects_checkpoint_mutation_before_next_training_effect(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_request, manifest = _request(tmp_path, config, step_index=0, max_steps=2)
    first = trainer.execute_request(
        first_request,
        config=config,
        base_manifest=manifest,
        train_step=_fake_train_step,
    )
    resume_state = first["resume_state"]
    assert isinstance(resume_state, dict)
    checkpoint = config.output_root / ("5" * 64) / "checkpoint-00000001"
    (checkpoint / "optimizer.pt").write_bytes(b"tampered")

    second_request, second_manifest = _request(
        tmp_path,
        config,
        step_index=1,
        max_steps=2,
        resume_state=resume_state,
    )
    called = False

    def should_not_run(**kwargs: object) -> tuple[int, str]:
        nonlocal called
        called = True
        return 0, "0" * 64

    with pytest.raises(trainer.WorkerInputError, match="checkpoint bytes"):
        trainer.execute_request(
            second_request,
            config=config,
            base_manifest=second_manifest,
            train_step=should_not_run,
        )
    assert called is False


def test_resume_rejects_wrong_step_binding(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request, _ = _request(
        tmp_path,
        config,
        step_index=1,
        max_steps=2,
        resume_state={
            "schema_version": 1,
            "completed_steps": 0,
            "checkpoint_id": "checkpoint-00000000",
            "checkpoint_manifest_sha256": "6" * 64,
            "next_record_index": 0,
        },
    )
    with pytest.raises(trainer.WorkerInputError, match="resume step"):
        trainer._validate_request(request)


def test_tree_digest_changes_with_relative_path_and_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.bin").write_bytes(b"x")
    (second / "b.bin").write_bytes(b"x")
    assert trainer.tree_sha256(first) != trainer.tree_sha256(second)
    (second / "b.bin").write_bytes(b"y")
    changed = trainer.tree_sha256(second)
    (second / "b.bin").write_bytes(b"x")
    assert trainer.tree_sha256(second) != changed


def test_package_versions_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {
        "torch": "2.7.0",
        "transformers": "5.17.0",
        "peft": "0.21.0",
    }
    monkeypatch.setattr(trainer.importlib.metadata, "version", lambda name: observed[name])

    class Config:
        torch_version = "2.7.0"
        transformers_version = "5.17.0"
        peft_version = "0.21.0"

    trainer._verify_backend_versions(Config())
    observed["peft"] = "0.21.1"
    with pytest.raises(trainer.WorkerInputError, match="version mismatch"):
        trainer._verify_backend_versions(Config())
