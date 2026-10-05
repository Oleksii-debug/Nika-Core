from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.training_peft_worker as peft
from nika_core.artifacts import ArtifactLocationKind, ArtifactRecord


_RUNTIME_VERSIONS = {
    "torch": "2.14.1",
    "transformers": "5.18.2",
    "peft": "0.21.2",
    "accelerate": "1.15.3",
    "gguf": "0.19.1",
    "safetensors": "0.8.2",
}


def _runtime_metadata(
    versions: dict[str, str] | None = None,
) -> dict[str, str]:
    selected = _RUNTIME_VERSIONS if versions is None else versions
    return {
        peft._TRAINING_RUNTIME_METADATA_KEYS[distribution]: selected[distribution]
        for distribution, _ in peft._TRAINING_RUNTIME_DISTRIBUTIONS
    }


def _trainer_artifact(
    tmp_path: Path,
    *,
    metadata: dict[str, str] | None = None,
) -> ArtifactRecord:
    trainer = tmp_path / "nika-peft-trainer.exe"
    trainer.write_bytes(b"registry-authorized-peft-trainer")
    payload = trainer.read_bytes()
    return ArtifactRecord(
        artifact_id="a" * 64,
        idempotency_key="peft-trainer",
        workspace_id="peft-tests",
        kind="training_executable",
        location_kind=ArtifactLocationKind.LOCAL_FILE,
        locator=str(trainer.resolve()),
        sha256=_sha256(payload),
        size_bytes=len(payload),
        metadata=_runtime_metadata() if metadata is None else dict(metadata),
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _body(prompt: str, response: str) -> bytes:
    return (
        json.dumps(
            {"prompt": prompt, "response": response},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _attestation(materials: list[dict[str, object]]) -> str:
    observations = [
        {
            "artifact_sha256": item["artifact_sha256"],
            "byte_count": item["byte_count"],
            "split": item["split"],
        }
        for item in materials
    ]
    encoded = json.dumps(
        observations,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(
        b"nika-training-consumed-materials-v1\x00" + encoded
    ).hexdigest()


def _request(tmp_path: Path, *, max_steps: int = 2) -> tuple[dict[str, object], bytes]:
    training = _body("train", "answer")
    validation = _body("validate", "answer")
    training_path = tmp_path / "training.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    training_path.write_bytes(training)
    validation_path.write_bytes(validation)
    materials: list[dict[str, object]] = [
        {
            "artifact_sha256": _sha256(training),
            "byte_count": len(training),
            "path": str(training_path),
            "split": "training",
        },
        {
            "artifact_sha256": _sha256(validation),
            "byte_count": len(validation),
            "path": str(validation_path),
            "split": "validation",
        },
    ]
    base = b"local-gguf"
    base_sha = _sha256(base)
    request: dict[str, object] = {
        "command_artifacts": [
            {
                "argument_index": 0,
                "artifact_id": "a" * 64,
                "sha256": "b" * 64,
            }
        ],
        "command_sha256": "c" * 64,
        "job": {
            "base_artifact": {
                "artifact_ref": "models/base",
                "sha256": base_sha,
            },
            "candidate_artifact_ref": "models/candidate/one",
            "command_sha256": "c" * 64,
            "frozen_package_sha256": "d" * 64,
            "job_id": "job-1",
            "max_steps": max_steps,
            "owner_id": "owner-1",
            "project_id": "project-1",
            "resource_scope": "training",
            "scale_authorization_sha256": "e" * 64,
            "task_id": "task-1",
            "training_material_sha256": "f" * 64,
        },
        "job_fingerprint": "1" * 64,
        "previous_step_id": None,
        "protocol_version": 3,
        "resume_state": {},
        "step_id": "2" * 64,
        "step_index": 0,
        "trainer_artifact_id": "a" * 64,
        "trainer_sha256": "b" * 64,
        "training_materials": {
            "base_artifact_sha256": base_sha,
            "materials": materials,
            "package_manifest_sha256": "d" * 64,
            "required_consumed_materials_sha256": _attestation(materials),
            "training_material_sha256": "f" * 64,
        },
    }
    return request, base


def _parsed(tmp_path: Path, *, max_steps: int = 2) -> tuple[peft.ParsedRequest, bytes]:
    request, base = _request(tmp_path, max_steps=max_steps)
    return peft._parse_request(request), base


def _config(tmp_path: Path, request: peft.ParsedRequest, base: bytes) -> peft.TrainerConfig:
    base_path = tmp_path / "base.gguf"
    base_path.write_bytes(base)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    output_root = tmp_path / "output"
    output_root.mkdir()
    return peft.TrainerConfig(
        base_gguf=base_path,
        model_dir=model_dir,
        model_dir_manifest_sha256=peft.model_directory_manifest_sha256(model_dir),
        trainer_implementation_sha256=peft.trainer_implementation_sha256(),
        training_runtime_versions=tuple(_RUNTIME_VERSIONS.items()),
        output_root=output_root,
        max_records=100,
        max_sequence_length=64,
        learning_rate=2e-4,
        lora_r=4,
        lora_alpha=8,
        lora_dropout=0.0,
        lora_target_modules=("q_proj", "v_proj"),
        torch_num_threads=2,
        seed=7,
    )


def test_protocol_v3_materials_are_rehashed_and_parsed(tmp_path: Path) -> None:
    request, _ = _parsed(tmp_path)
    consumed = peft._consume_materials(request, max_records=10)

    assert consumed.attestation_sha256 == request.required_consumed_materials_sha256
    assert consumed.training == (peft.TrainingExample("train", "answer"),)
    assert consumed.validation == (peft.TrainingExample("validate", "answer"),)


def test_material_tamper_fails_before_training(tmp_path: Path) -> None:
    request, _ = _parsed(tmp_path)
    request.materials[0].path.write_bytes(_body("other", "bytes"))

    with pytest.raises(peft.PeftTrainerError, match="material_"):
        peft._consume_materials(request, max_records=10)


def test_dataset_unknown_fields_fail_closed(tmp_path: Path) -> None:
    raw_request, _ = _request(tmp_path)
    materials = raw_request["training_materials"]
    assert isinstance(materials, dict)
    rows = materials["materials"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    path = Path(str(first["path"]))
    bad = b'{"prompt":"p","response":"r","secret":"x"}\n'
    path.write_bytes(bad)
    first["artifact_sha256"] = _sha256(bad)
    first["byte_count"] = len(bad)
    materials["required_consumed_materials_sha256"] = _attestation(rows)
    request = peft._parse_request(raw_request)

    with pytest.raises(peft.PeftTrainerError, match="dataset_record_fields_invalid"):
        peft._consume_materials(request, max_records=10)


def test_record_limit_rejects_instead_of_truncating(tmp_path: Path) -> None:
    raw_request, _ = _request(tmp_path)
    materials = raw_request["training_materials"]
    assert isinstance(materials, dict)
    rows = materials["materials"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    path = Path(str(first["path"]))
    body = _body("one", "a") + _body("two", "b")
    path.write_bytes(body)
    first["artifact_sha256"] = _sha256(body)
    first["byte_count"] = len(body)
    materials["required_consumed_materials_sha256"] = _attestation(rows)
    request = peft._parse_request(raw_request)

    with pytest.raises(peft.PeftTrainerError, match="dataset_record_limit_exceeded"):
        peft._consume_materials(request, max_records=2)


def test_model_directory_manifest_binds_content_and_paths(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"a":1}', encoding="utf-8")
    first = peft.model_directory_manifest_sha256(model_dir)

    (model_dir / "config.json").write_text('{"a":2}', encoding="utf-8")
    second = peft.model_directory_manifest_sha256(model_dir)

    assert first != second


def test_model_directory_manifest_rejects_path_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    source = model_dir / "config.json"
    source.write_text('{"a":1}', encoding="utf-8")
    replacement = model_dir / "replacement.json"
    replacement.write_text('{"a":2}', encoding="utf-8")
    real_open = peft.os.open
    substituted = False

    def substituting_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal substituted
        if Path(path) == source and not substituted:
            substituted = True
            return real_open(replacement, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(peft.os, "open", substituting_open)

    with pytest.raises(ValueError, match="changed before hashing"):
        peft.model_directory_manifest_sha256(model_dir)

    assert substituted is True


def test_model_directory_manifest_rejects_casefold_collision(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "Config.json").write_text('{"a":1}', encoding="utf-8")
    (model_dir / "config.json").write_text('{"a":1}', encoding="utf-8")

    with pytest.raises(ValueError, match="case-fold path collisions"):
        peft.model_directory_manifest_sha256(model_dir)


def test_model_directory_snapshot_detaches_live_source(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)
    job_root.mkdir(parents=True)

    snapshot = peft._model_directory_snapshot(config, job_root)
    original = (snapshot / "config.json").read_bytes()

    (config.model_dir / "config.json").write_text('{"changed":true}', encoding="utf-8")

    assert snapshot.name == "model-snapshot"
    assert (snapshot / "config.json").read_bytes() == original
    assert peft.model_directory_manifest_sha256(snapshot) == config.model_dir_manifest_sha256


def test_model_directory_snapshot_rejects_source_replacement_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)
    job_root.mkdir(parents=True)
    source = config.model_dir / "config.json"
    original = source.with_name("config.original.json")
    real_read = peft.os.read
    replaced = False

    def swapping_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(fd, size)
        if chunk and not replaced:
            replaced = True
            source.replace(original)
            source.write_text('{"replacement":true}', encoding="utf-8")
        return chunk

    monkeypatch.setattr(peft.os, "read", swapping_read)

    with pytest.raises(peft.PeftTrainerError, match="model_dir_source_changed"):
        peft._model_directory_snapshot(config, job_root)

    assert replaced is True


def test_model_directory_snapshot_detects_persisted_tamper(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)
    job_root.mkdir(parents=True)

    snapshot = peft._model_directory_snapshot(config, job_root)
    (snapshot / "config.json").write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(peft.PeftTrainerError, match="model_dir_snapshot_mismatch"):
        peft._model_directory_snapshot(config, job_root)


def test_candidate_path_is_stable_and_reference_specific(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    first = peft.candidate_artifact_path(root, "models/candidate/a")
    same = peft.candidate_artifact_path(root, "models/candidate/a")
    other = peft.candidate_artifact_path(root, "models/candidate/b")

    assert first == same
    assert first != other
    assert first.name == "adapter_model.safetensors"
    assert root in first.parents


def test_request_rejects_private_local_artifact_refs(tmp_path: Path) -> None:
    raw_request, _ = _request(tmp_path)
    raw_request["job"]["base_artifact"]["artifact_ref"] = "C:/private/base.gguf"
    with pytest.raises(peft.PeftTrainerError, match="artifact_ref_private_path"):
        peft._parse_request(raw_request)

    raw_request, _ = _request(tmp_path)
    raw_request["job"]["candidate_artifact_ref"] = "/private/candidate"
    with pytest.raises(peft.PeftTrainerError, match="artifact_ref_private_path"):
        peft._parse_request(raw_request)


def test_base_gguf_copy_is_digest_bound(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)

    staged = peft._copy_verified_base(config, request, job_root)
    assert staged.read_bytes() == base

    config.base_gguf.write_bytes(b"wrong")
    with pytest.raises(peft.PeftTrainerError, match="base_gguf_digest_mismatch"):
        peft._copy_verified_base(config, request, tmp_path / "other-job")


def test_resume_marker_binds_job_step_and_consumed_materials(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)
    checkpoint = peft._checkpoint_dir(job_root, 1)
    checkpoint.mkdir(parents=True)
    (checkpoint / "optimizer.pt").write_bytes(b"optimizer-state")
    payload_sha = peft._checkpoint_payload_manifest_sha256(checkpoint)
    marker_sha = peft._write_checkpoint_marker(
        checkpoint,
        request=request,
        consumed_sha256=request.required_consumed_materials_sha256,
        checkpoint_payload_sha256=payload_sha,
    )
    resume = {
        "checkpoint_marker_sha256": marker_sha,
        "checkpoint_payload_sha256": payload_sha,
        "checkpoint_step": 1,
        "job_fingerprint": request.job_fingerprint,
        "relative_path": checkpoint.relative_to(job_root).as_posix(),
        "schema_version": 1,
    }
    raw_request, _ = _request(tmp_path)
    raw_request["step_index"] = 1
    raw_request["previous_step_id"] = request.step_id
    raw_request["step_id"] = "3" * 64
    raw_request["resume_state"] = resume
    second = peft._parse_request(raw_request)

    assert peft._resume_checkpoint(job_root, second) == checkpoint.resolve()

    resume["checkpoint_marker_sha256"] = "0" * 64
    raw_request["resume_state"] = resume
    tampered = peft._parse_request(raw_request)
    with pytest.raises(peft.PeftTrainerError, match="resume_marker_digest_mismatch"):
        peft._resume_checkpoint(job_root, tampered)


def test_resume_rejects_tampered_checkpoint_payload(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    job_root = peft._job_root(config, request)
    checkpoint = peft._checkpoint_dir(job_root, 1)
    checkpoint.mkdir(parents=True)
    optimizer = checkpoint / "optimizer.pt"
    optimizer.write_bytes(b"optimizer-state")
    payload_sha = peft._checkpoint_payload_manifest_sha256(checkpoint)
    marker_sha = peft._write_checkpoint_marker(
        checkpoint,
        request=request,
        consumed_sha256=request.required_consumed_materials_sha256,
        checkpoint_payload_sha256=payload_sha,
    )
    resume = {
        "checkpoint_marker_sha256": marker_sha,
        "checkpoint_payload_sha256": payload_sha,
        "checkpoint_step": 1,
        "job_fingerprint": request.job_fingerprint,
        "relative_path": checkpoint.relative_to(job_root).as_posix(),
        "schema_version": 1,
    }
    raw_request, _ = _request(tmp_path)
    raw_request["step_index"] = 1
    raw_request["previous_step_id"] = request.step_id
    raw_request["step_id"] = "3" * 64
    raw_request["resume_state"] = resume
    second = peft._parse_request(raw_request)

    optimizer.write_bytes(b"forged-optimizer-state")

    with pytest.raises(peft.PeftTrainerError, match="resume_checkpoint_payload_mismatch"):
        peft._resume_checkpoint(job_root, second)


class _FakeTokenizer:
    eos_token = "<eos>"
    eos_token_id = 1
    pad_token_id = None
    pad_token = None

    def __call__(
        self,
        text: str,
        *,
        truncation: bool,
        max_length: int,
        add_special_tokens: bool,
    ) -> dict[str, list[int]]:
        assert truncation is True
        assert add_special_tokens is True
        ids = [min(ord(char), 255) for char in text][:max_length]
        return {"attention_mask": [1] * len(ids), "input_ids": ids}


def test_response_tokens_must_survive_sequence_budget() -> None:
    tokenizer = _FakeTokenizer()
    long_prompt = "p" * 128

    with pytest.raises(peft.PeftTrainerError, match="response_tokens_truncated"):
        peft._TokenizedDataset(
            (peft.TrainingExample(long_prompt, "answer"),),
            tokenizer,
            32,
        )


class _FakeTokenizerFactory:
    @staticmethod
    def from_pretrained(*args: object, **kwargs: object) -> _FakeTokenizer:
        assert Path(str(args[0])).name == "model-snapshot"
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
        assert str(kwargs["gguf_file"]).endswith("base.gguf")
        return _FakeTokenizer()


class _FakeModel:
    def save_pretrained(self, path: str, *, safe_serialization: bool) -> None:
        assert safe_serialization is True
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        (target / "adapter_model.safetensors").write_bytes(b"real-adapter-weights")
        (target / "adapter_config.json").write_text(
            (
                '{"base_model_name_or_path":"C:/private/model","bias":"none",'
                '"lora_alpha":8,"lora_dropout":0.0,"r":4,'
                '"target_modules":["q_proj","v_proj"],"task_type":"CAUSAL_LM"}'
            ),
            encoding="utf-8",
        )


class _FakeSafeTensorReader:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def __enter__(self) -> "_FakeSafeTensorReader":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def keys(self) -> list[str]:
        return ["lora.weight"]

    def get_tensor(self, name: str) -> bytes:
        assert name == "lora.weight"
        return b"tensor-bytes"


def _fake_safe_open(path: str, *, framework: str, device: str) -> _FakeSafeTensorReader:
    assert framework == "pt"
    assert device == "cpu"
    return _FakeSafeTensorReader(path)


def _fake_safe_save_file(
    tensors: dict[str, object],
    path: str,
    *,
    metadata: dict[str, str],
) -> None:
    assert list(tensors) == ["lora.weight"]
    payload = json.dumps(
        {"metadata": metadata, "tensor_names": list(tensors)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    Path(path).write_bytes(payload)


class _FakeModelFactory:
    @staticmethod
    def from_pretrained(*args: object, **kwargs: object) -> _FakeModel:
        assert Path(str(args[0])).name == "model-snapshot"
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
        assert kwargs["dtype"] == "auto"
        return _FakeModel()


class _FakePeftModel:
    @staticmethod
    def from_pretrained(
        model: _FakeModel,
        path: str,
        *,
        is_trainable: bool,
        local_files_only: bool,
    ) -> _FakeModel:
        assert model is not None
        assert is_trainable is True
        assert local_files_only is True
        assert (Path(path) / "adapter_model.safetensors").is_file()
        return _FakeModel()


class _FakeLoraConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeTaskType:
    CAUSAL_LM = "CAUSAL_LM"


def _fake_get_peft_model(model: _FakeModel, config: _FakeLoraConfig) -> _FakeModel:
    assert config.kwargs["task_type"] == "CAUSAL_LM"
    return model


class _FakeCollator:
    def __init__(self, **kwargs: object) -> None:
        assert kwargs["mlm"] is False


class _FakeTrainingArguments:
    def __init__(self, **kwargs: object) -> None:
        self.output_dir = kwargs["output_dir"]
        self.max_steps = kwargs["max_steps"]
        assert kwargs["use_cpu"] is True
        assert kwargs["full_determinism"] is True
        assert kwargs["dataloader_num_workers"] == 0
        assert kwargs["dataloader_pin_memory"] is False
        assert kwargs["optim"] == "adamw_torch"


class _FakeTrainer:
    def __init__(
        self,
        *,
        model: _FakeModel,
        args: _FakeTrainingArguments,
        **kwargs: object,
    ) -> None:
        self.model = model
        self.args = args
        assert len(kwargs["train_dataset"]) == 1
        assert len(kwargs["eval_dataset"]) == 1

    def train(self, *, resume_from_checkpoint: str | bool) -> None:
        if self.args.max_steps == 1:
            assert resume_from_checkpoint is False
        else:
            assert Path(str(resume_from_checkpoint)).name == "checkpoint-1"
        checkpoint = Path(self.args.output_dir) / f"checkpoint-{self.args.max_steps}"
        checkpoint.mkdir(parents=True, exist_ok=True)


def _fake_stack() -> tuple[object, ...]:
    torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: False,
            empty_cache=lambda: None,
        ),
        set_num_threads=lambda value: value == 2
        or (_ for _ in ()).throw(AssertionError("unexpected torch thread count")),
        use_deterministic_algorithms=lambda enabled: enabled is True
        or (_ for _ in ()).throw(AssertionError("determinism must be enabled")),
    )
    return (
        torch,
        _FakeLoraConfig,
        _FakePeftModel,
        _FakeTaskType,
        _fake_get_peft_model,
        _fake_safe_open,
        _fake_safe_save_file,
        _FakeModelFactory,
        _FakeTokenizerFactory,
        _FakeCollator,
        _FakeTrainer,
        _FakeTrainingArguments,
        lambda seed: None,
    )


def test_fake_stack_proves_step_resume_and_final_safetensors_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path, max_steps=2)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)

    first_state, first_candidate = peft._train_one_step(request, config, consumed)
    assert first_candidate is None
    assert first_state["checkpoint_step"] == 1

    raw_second, _ = _request(tmp_path, max_steps=2)
    raw_second["step_index"] = 1
    raw_second["previous_step_id"] = request.step_id
    raw_second["step_id"] = "3" * 64
    raw_second["resume_state"] = first_state
    second = peft._parse_request(raw_second)
    second_consumed = peft._consume_materials(second, max_records=10)

    second_state, candidate_sha256 = peft._train_one_step(
        second,
        config,
        second_consumed,
    )

    candidate = peft.candidate_artifact_path(
        config.output_root,
        second.candidate_artifact_ref,
    )
    candidate_bytes = candidate.read_bytes()
    assert b"nika_adapter_manifest" in candidate_bytes
    assert candidate_sha256 == _sha256(candidate_bytes)
    assert b"models/base" in candidate_bytes
    assert b"C:/private/model" not in candidate_bytes
    assert second_state["checkpoint_step"] == 2


def test_final_candidate_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"existing")
    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_conflict"):
        peft._train_one_step(request, config, consumed)

    assert candidate.read_bytes() == b"existing"


def test_environment_builder_binds_implementation_model_dir_and_hyperparameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    monkeypatch.setattr(
        peft.importlib.metadata,
        "version",
        _RUNTIME_VERSIONS.__getitem__,
    )
    trainer_artifact = _trainer_artifact(tmp_path)
    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=trainer_artifact,
        max_records=123,
        max_sequence_length=256,
        learning_rate=0.0003,
        lora_r=16,
        lora_alpha=32,
        lora_dropout=0.1,
        lora_target_modules=("q_proj", "k_proj", "v_proj"),
        torch_num_threads=3,
        seed=99,
    )

    assert environment["NIKA_TRAINER_IMPLEMENTATION_SHA256"] == (
        peft.trainer_implementation_sha256()
    )
    assert environment["NIKA_TRAINER_MODEL_DIR_MANIFEST_SHA256"] == (
        peft.model_directory_manifest_sha256(config.model_dir)
    )
    assert environment["NIKA_TRAINER_LORA_TARGET_MODULES"] == "q_proj,k_proj,v_proj"
    assert environment["NIKA_TRAINER_MAX_RECORDS"] == "123"
    assert environment["NIKA_TRAINER_TORCH_NUM_THREADS"] == "3"
    assert environment["NIKA_TRAINER_RUNTIME_MANIFEST_SHA256"] == (
        peft._training_runtime_manifest_sha256(dict(_RUNTIME_VERSIONS))
    )
    assert environment["NIKA_TRAINER_DEPLOYMENT_ARTIFACT_ID"] == "a" * 64
    assert environment["NIKA_TRAINER_DEPLOYMENT_SHA256"] == trainer_artifact.sha256
    for distribution, environment_key in peft._TRAINING_RUNTIME_DISTRIBUTIONS:
        assert environment[environment_key] == _RUNTIME_VERSIONS[distribution]

    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    loaded = peft._read_config()
    assert loaded.model_dir_manifest_sha256 == environment[
        "NIKA_TRAINER_MODEL_DIR_MANIFEST_SHA256"
    ]
    assert loaded.trainer_implementation_sha256 == peft.trainer_implementation_sha256()
    assert dict(loaded.training_runtime_versions) == _RUNTIME_VERSIONS
    assert loaded.lora_r == 16
    assert loaded.torch_num_threads == 3
    assert loaded.seed == 99


def test_trainer_deployment_identity_binds_exact_registry_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, _ = _request(tmp_path)
    trainer_artifact = _trainer_artifact(tmp_path)
    raw_request["trainer_artifact_id"] = trainer_artifact.artifact_id
    raw_request["trainer_sha256"] = trainer_artifact.sha256
    request = peft._parse_request(raw_request)
    monkeypatch.setenv(
        "NIKA_TRAINER_DEPLOYMENT_ARTIFACT_ID",
        trainer_artifact.artifact_id,
    )
    monkeypatch.setenv(
        "NIKA_TRAINER_DEPLOYMENT_SHA256",
        trainer_artifact.sha256,
    )

    peft._verify_trainer_deployment_identity(request)

    monkeypatch.setenv("NIKA_TRAINER_DEPLOYMENT_SHA256", "0" * 64)
    with pytest.raises(peft.PeftTrainerError, match="deployment_sha256_mismatch"):
        peft._verify_trainer_deployment_identity(request)


def test_environment_builder_rejects_invalid_torch_thread_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    monkeypatch.setattr(
        peft.importlib.metadata,
        "version",
        _RUNTIME_VERSIONS.__getitem__,
    )

    with pytest.raises(ValueError, match="torch_num_threads"):
        peft.build_trainer_environment(
            base_gguf=config.base_gguf,
            model_dir=config.model_dir,
            output_root=config.output_root,
            trainer_artifact=_trainer_artifact(tmp_path),
            torch_num_threads=0,
        )


def test_read_config_rejects_training_runtime_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    monkeypatch.setattr(
        peft.importlib.metadata,
        "version",
        _RUNTIME_VERSIONS.__getitem__,
    )
    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=_trainer_artifact(tmp_path),
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    drifted = dict(_RUNTIME_VERSIONS)
    drifted["transformers"] = "5.18.3"
    monkeypatch.setattr(peft.importlib.metadata, "version", drifted.__getitem__)

    with pytest.raises(peft.PeftTrainerError, match="runtime_version_mismatch"):
        peft._read_config()


def test_environment_builder_does_not_probe_parent_runtime_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)

    def unexpected(_: str) -> str:
        raise AssertionError("parent package metadata must not be runtime authority")

    monkeypatch.setattr(peft.importlib.metadata, "version", unexpected)

    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=_trainer_artifact(tmp_path),
    )

    assert environment["NIKA_TRAINER_TORCH_VERSION"] == _RUNTIME_VERSIONS["torch"]


def test_read_config_rejects_missing_training_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=_trainer_artifact(tmp_path),
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def missing(distribution: str) -> str:
        raise peft.importlib.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(peft.importlib.metadata, "version", missing)

    with pytest.raises(peft.PeftTrainerError, match="runtime_versions_unavailable"):
        peft._read_config()


def test_environment_builder_rejects_incomplete_runtime_manifest(
    tmp_path: Path,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    incomplete = _runtime_metadata()
    incomplete.pop(peft._TRAINING_RUNTIME_METADATA_KEYS["gguf"])

    with pytest.raises(ValueError, match="trainer deployment runtime metadata"):
        peft.build_trainer_environment(
            base_gguf=config.base_gguf,
            model_dir=config.model_dir,
            output_root=config.output_root,
            trainer_artifact=_trainer_artifact(tmp_path, metadata=incomplete),
        )


def test_environment_builder_rejects_ambiguous_registry_runtime_metadata(
    tmp_path: Path,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    ambiguous = _runtime_metadata()
    ambiguous["nika.training.runtime.unexpected.version"] = "1.0"

    with pytest.raises(ValueError, match="trainer deployment runtime metadata"):
        peft.build_trainer_environment(
            base_gguf=config.base_gguf,
            model_dir=config.model_dir,
            output_root=config.output_root,
            trainer_artifact=_trainer_artifact(tmp_path, metadata=ambiguous),
        )


def test_environment_builder_allows_unrelated_registry_metadata(
    tmp_path: Path,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    metadata = _runtime_metadata()
    metadata["nika.training.provenance"] = "fixture"

    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=_trainer_artifact(tmp_path, metadata=metadata),
    )

    assert environment["NIKA_TRAINER_DEPLOYMENT_ARTIFACT_ID"] == "a" * 64


def test_read_config_rejects_runtime_manifest_digest_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        trainer_artifact=_trainer_artifact(tmp_path),
    )
    environment["NIKA_TRAINER_TORCH_VERSION"] = "2.99.0"
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(peft.PeftTrainerError, match="runtime_manifest_mismatch"):
        peft._read_config()


def test_candidate_manifest_semantics_fail_closed_on_tampering(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    adapter_config = {
        "base_model_name_or_path": request.base_artifact_ref,
        "bias": "none",
        "lora_alpha": config.lora_alpha,
        "lora_dropout": config.lora_dropout,
        "r": config.lora_r,
        "target_modules": list(config.lora_target_modules),
        "task_type": "CAUSAL_LM",
    }
    raw = peft._candidate_manifest_json(
        request=request,
        config=config,
        consumed=consumed,
        adapter_config=adapter_config,
    )
    manifest = json.loads(raw)
    assert peft._validate_candidate_manifest_payload(manifest) == manifest
    assert manifest["trainer_artifact_id"] == request.trainer_artifact_id
    assert manifest["trainer_sha256"] == request.trainer_sha256
    assert manifest["trainer_implementation_sha256"] == config.trainer_implementation_sha256
    assert manifest["training_runtime_versions"] == _RUNTIME_VERSIONS
    assert manifest["training_runtime_manifest_sha256"] == (
        peft._training_runtime_manifest_sha256(_RUNTIME_VERSIONS)
    )
    assert manifest["trainer_parameters"]["torch_num_threads"] == config.torch_num_threads

    bad_sha = json.loads(raw)
    bad_sha["base_artifact_sha256"] = "0" * 63
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_sha)

    bad_trainer_sha = json.loads(raw)
    bad_trainer_sha["trainer_sha256"] = "0" * 63
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_trainer_sha)

    bad_implementation_sha = json.loads(raw)
    bad_implementation_sha["trainer_implementation_sha256"] = "0" * 63
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_implementation_sha)

    bad_runtime_version = json.loads(raw)
    bad_runtime_version["training_runtime_versions"]["torch"] = "2.99.0"
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_runtime_version)

    missing_runtime_version = json.loads(raw)
    del missing_runtime_version["training_runtime_versions"]["gguf"]
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(missing_runtime_version)

    bad_runtime_manifest = json.loads(raw)
    bad_runtime_manifest["training_runtime_manifest_sha256"] = "0" * 64
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_runtime_manifest)

    bad_parameter = json.loads(raw)
    bad_parameter["trainer_parameters"]["lora_r"] = config.lora_r + 1
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_parameter)

    bad_thread_count = json.loads(raw)
    bad_thread_count["trainer_parameters"]["torch_num_threads"] = 0
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_thread_count)

    missing_thread_count = json.loads(raw)
    del missing_thread_count["trainer_parameters"]["torch_num_threads"]
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(missing_thread_count)

    private_ref = json.loads(raw)
    private_ref["base_artifact_ref"] = "C:/private/base.gguf"
    private_ref["adapter_config"]["base_model_name_or_path"] = "C:/private/base.gguf"
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(private_ref)

    unknown_field = json.loads(raw)
    unknown_field["unexpected"] = True
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(unknown_field)

    control_ref = json.loads(raw)
    control_ref["candidate_artifact_ref"] = "models/candidate\nforged"
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(control_ref)

    duplicate_adapter_target = json.loads(raw)
    first_target = duplicate_adapter_target["adapter_config"]["target_modules"][0]
    duplicate_adapter_target["adapter_config"]["target_modules"].append(first_target)
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(duplicate_adapter_target)


def test_candidate_manifest_producer_rejects_reader_invalid_adapter_config(
    tmp_path: Path,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    adapter_config = {
        "base_model_name_or_path": request.base_artifact_ref,
        "bias": "none",
        "lora_alpha": config.lora_alpha,
        "lora_dropout": config.lora_dropout,
        "r": config.lora_r,
        "target_modules": ["q_proj", "q_proj", "v_proj"],
        "task_type": "CAUSAL_LM",
    }

    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._candidate_manifest_json(
            request=request,
            config=config,
            consumed=consumed,
            adapter_config=adapter_config,
        )


def test_candidate_manifest_rejects_unhashable_target_carrier(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    adapter_config = {
        "base_model_name_or_path": request.base_artifact_ref,
        "bias": "none",
        "lora_alpha": config.lora_alpha,
        "lora_dropout": config.lora_dropout,
        "r": config.lora_r,
        "target_modules": list(config.lora_target_modules),
        "task_type": "CAUSAL_LM",
    }
    manifest = json.loads(
        peft._candidate_manifest_json(
            request=request,
            config=config,
            consumed=consumed,
            adapter_config=adapter_config,
        )
    )
    manifest["trainer_parameters"]["lora_target_modules"] = [["q_proj"]]

    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(manifest)


def test_adapter_config_snapshot_rejects_training_plan_mismatch(tmp_path: Path) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    adapter_dir = tmp_path / "adapter-mismatch"
    adapter_dir.mkdir()
    (adapter_dir / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "C:/private/model",
                "bias": "none",
                "lora_alpha": 8,
                "lora_dropout": 0.0,
                "r": 99,
                "target_modules": ["q_proj", "v_proj"],
                "task_type": "CAUSAL_LM",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(peft.PeftTrainerError, match="adapter_config_training_plan_mismatch"):
        peft._adapter_config_snapshot(adapter_dir, request, config)


def test_adapter_config_snapshot_removes_private_base_path_and_rejects_other_paths(
    tmp_path: Path,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    adapter_dir = tmp_path / "adapter-private"
    adapter_dir.mkdir()
    payload = {
        "base_model_name_or_path": "C:/private/model",
        "bias": "none",
        "lora_alpha": 8,
        "lora_dropout": 0.0,
        "r": 4,
        "target_modules": ["q_proj", "v_proj"],
        "task_type": "CAUSAL_LM",
    }
    path = adapter_dir / "adapter_config.json"
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    snapshot = peft._adapter_config_snapshot(adapter_dir, request, config)
    assert snapshot["base_model_name_or_path"] == request.base_artifact_ref
    assert "C:/private/model" not in json.dumps(snapshot)

    payload["modules_to_save"] = ["C:/private/other"]
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(peft.PeftTrainerError, match="adapter_config_private_path"):
        peft._adapter_config_snapshot(adapter_dir, request, config)


def test_final_candidate_publish_race_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    real_link = peft.os.link
    raced = False

    def _racing_link(source: object, target: object, *args: object, **kwargs: object) -> None:
        nonlocal raced
        target_path = Path(target)
        if target_path == candidate and not raced:
            raced = True
            target_path.write_bytes(b"competitor")
        real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft.os, "link", _racing_link)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_conflict"):
        peft._train_one_step(request, config, consumed)

    assert raced is True
    assert candidate.read_bytes() == b"competitor"


def test_final_candidate_immediate_post_link_substitution_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    real_link = peft.os.link
    substituted = False

    def _substituting_link(
        source: object,
        target: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal substituted
        real_link(source, target, *args, **kwargs)
        target_path = Path(target)
        if target_path == candidate:
            target_path.unlink()
            target_path.write_bytes(b"substituted")
            substituted = True

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft.os, "link", _substituting_link)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_digest_mismatch"):
        peft._train_one_step(request, config, consumed)

    assert substituted is True
    assert candidate.read_bytes() == b"substituted"

def test_final_candidate_uses_unique_reserved_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    candidate.parent.mkdir(parents=True)
    legacy_temporary = candidate.parent / ".adapter_model.safetensors.tmp"
    legacy_temporary.write_bytes(b"other-attempt")
    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)

    _, candidate_sha256 = peft._train_one_step(request, config, consumed)

    assert candidate_sha256 == _sha256(candidate.read_bytes())
    assert legacy_temporary.read_bytes() == b"other-attempt"
    assert sorted(
        path.name
        for path in candidate.parent.iterdir()
        if path.name.endswith(".tmp")
    ) == [legacy_temporary.name]

def test_final_candidate_cleanup_failure_rolls_back_published_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    real_unlink = peft.os.unlink

    def _unlink(path: object, *args: object, **kwargs: object) -> None:
        value = Path(path)
        if (
            value.parent == candidate.parent
            and value.name.startswith(".adapter_model.safetensors.")
            and value.name.endswith(".tmp")
        ):
            raise PermissionError("simulated temporary cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft.os, "unlink", _unlink)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_cleanup_failed"):
        peft._train_one_step(request, config, consumed)

    assert not candidate.exists()


def test_final_candidate_rejects_extra_hardlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    alias = candidate.parent / "external-alias.safetensors"
    real_link = peft.os.link

    def _link(source: object, target: object, *args: object, **kwargs: object) -> None:
        real_link(source, target, *args, **kwargs)
        if Path(target) == candidate:
            real_link(source, alias)

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft.os, "link", _link)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_digest_mismatch"):
        peft._train_one_step(request, config, consumed)

    assert not candidate.exists()
    assert alias.exists()

def test_final_candidate_rejects_checkpoint_change_during_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    checkpoint = peft._checkpoint_dir(peft._job_root(config, request), 1)
    adapter_file = checkpoint / "adapter" / "adapter_model.safetensors"
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )

    def _mutating_safe_save_file(
        tensors: dict[str, object],
        path: str,
        *,
        metadata: dict[str, str],
    ) -> None:
        adapter_file.write_bytes(adapter_file.read_bytes() + b"-tampered")
        _fake_safe_save_file(tensors, path, metadata=metadata)

    def _mutating_stack() -> tuple[object, ...]:
        values = list(_fake_stack())
        values[6] = _mutating_safe_save_file
        return tuple(values)

    monkeypatch.setattr(peft, "_import_training_stack", _mutating_stack)

    with pytest.raises(
        peft.PeftTrainerError,
        match="checkpoint_payload_changed_during_candidate",
    ):
        peft._train_one_step(request, config, consumed)

    assert not candidate.exists()

def test_final_candidate_rejects_checkpoint_change_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_request, base = _request(tmp_path, max_steps=1)
    request = peft._parse_request(raw_request)
    config = _config(tmp_path, request, base)
    consumed = peft._consume_materials(request, max_records=10)
    checkpoint = peft._checkpoint_dir(peft._job_root(config, request), 1)
    adapter_file = checkpoint / "adapter" / "adapter_model.safetensors"
    candidate = peft.candidate_artifact_path(
        config.output_root,
        request.candidate_artifact_ref,
    )
    real_unlink = peft.os.unlink
    mutated = False

    def _unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal mutated
        value = Path(path)
        real_unlink(path, *args, **kwargs)
        if (
            not mutated
            and value.parent == candidate.parent
            and value.name.startswith(".adapter_model.safetensors.")
            and value.name.endswith(".tmp")
        ):
            adapter_file.write_bytes(adapter_file.read_bytes() + b"-late-tamper")
            mutated = True

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft.os, "unlink", _unlink)

    with pytest.raises(
        peft.PeftTrainerError,
        match="checkpoint_payload_changed_after_candidate",
    ):
        peft._train_one_step(request, config, consumed)

    assert mutated is True
    assert not candidate.exists()

