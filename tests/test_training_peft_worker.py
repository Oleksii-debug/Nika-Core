from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.training_peft_worker as peft


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
        output_root=output_root,
        max_records=100,
        max_sequence_length=64,
        learning_rate=2e-4,
        lora_r=4,
        lora_alpha=8,
        lora_dropout=0.0,
        lora_target_modules=("q_proj", "v_proj"),
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


def test_candidate_path_is_stable_and_reference_specific(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    first = peft.candidate_artifact_path(root, "models/candidate/a")
    same = peft.candidate_artifact_path(root, "models/candidate/a")
    other = peft.candidate_artifact_path(root, "models/candidate/b")

    assert first == same
    assert first != other
    assert first.name == "adapter_model.safetensors"
    assert root in first.parents


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
        )
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


def test_concurrent_candidate_publish_never_overwrites_winner(
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
    real_publish = peft._publish_regular_file_no_replace

    def racing_publish(source: Path, target: Path) -> None:
        target.write_bytes(b"concurrent-winner")
        real_publish(source, target)

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft, "_publish_regular_file_no_replace", racing_publish)

    with pytest.raises(peft.PeftTrainerError, match="candidate_publish_conflict"):
        peft._train_one_step(request, config, consumed)

    assert candidate.read_bytes() == b"concurrent-winner"


def test_checkpoint_mutation_blocks_final_candidate_publication(
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
    real_snapshot = peft._adapter_config_snapshot

    def mutate_after_snapshot(
        path: Path,
        parsed: peft.ParsedRequest,
        trainer_config: peft.TrainerConfig,
    ) -> dict[str, object]:
        snapshot = real_snapshot(path, parsed, trainer_config)
        (path / "adapter_model.safetensors").write_bytes(b"post-marker-tamper")
        return snapshot

    monkeypatch.setattr(peft, "_import_training_stack", _fake_stack)
    monkeypatch.setattr(peft, "_adapter_config_snapshot", mutate_after_snapshot)

    with pytest.raises(
        peft.PeftTrainerError,
        match="checkpoint_payload_changed_before_publish",
    ):
        peft._train_one_step(request, config, consumed)

    assert not candidate.exists()


def test_environment_builder_binds_implementation_model_dir_and_hyperparameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, base = _parsed(tmp_path)
    config = _config(tmp_path, request, base)
    environment = peft.build_trainer_environment(
        base_gguf=config.base_gguf,
        model_dir=config.model_dir,
        output_root=config.output_root,
        max_records=123,
        max_sequence_length=256,
        learning_rate=0.0003,
        lora_r=16,
        lora_alpha=32,
        lora_dropout=0.1,
        lora_target_modules=("q_proj", "k_proj", "v_proj"),
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

    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    loaded = peft._read_config()
    assert loaded.model_dir_manifest_sha256 == environment[
        "NIKA_TRAINER_MODEL_DIR_MANIFEST_SHA256"
    ]
    assert loaded.lora_r == 16
    assert loaded.seed == 99


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

    bad_sha = json.loads(raw)
    bad_sha["base_artifact_sha256"] = "0" * 63
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_sha)

    bad_parameter = json.loads(raw)
    bad_parameter["trainer_parameters"]["lora_r"] = config.lora_r + 1
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(bad_parameter)

    private_ref = json.loads(raw)
    private_ref["base_artifact_ref"] = "C:/private/base.gguf"
    private_ref["adapter_config"]["base_model_name_or_path"] = "C:/private/base.gguf"
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(private_ref)

    unknown_field = json.loads(raw)
    unknown_field["unexpected"] = True
    with pytest.raises(peft.PeftTrainerError, match="candidate_manifest_invalid"):
        peft._validate_candidate_manifest_payload(unknown_field)


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
