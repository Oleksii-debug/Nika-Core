from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.training_physical_pilot_driver as driver


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "pilot-workspace",
        "project_id": "pilot-project",
        "owner_id": "pilot-owner",
        "job_id": "pilot-job",
        "blob_store_root": str(tmp_path / "blobs"),
        "frozen_package_path": str(tmp_path / "package.json"),
        "frozen_package_sha256": "1" * 64,
        "trainer_executable": str(tmp_path / "nika-peft-trainer.exe"),
        "base_artifact_ref": "models/base",
        "base_gguf_path": str(tmp_path / "base.gguf"),
        "model_dir": str(tmp_path / "model"),
        "output_root": str(tmp_path / "pilot-output"),
        "candidate_artifact_ref": "models/pilot-candidate",
        "candidate_descriptor": {
            "model_id": "nika-pilot-adapter",
            "source_reference": "https://example.com/models/base",
            "license_reference": "https://example.com/licenses/model",
        },
        "runtime_versions": {
            "torch": "2.14.1",
            "transformers": "5.18.2",
            "peft": "0.21.2",
            "accelerate": "1.15.0",
            "gguf": "0.19.0",
            "safetensors": "0.8.1",
        },
        "resource_budget": {
            "max_cpu_percent": 95,
            "max_memory_percent": 90,
        },
        "trainer_parameters": {
            "max_sequence_length": 256,
            "learning_rate": 0.0002,
            "lora_r": 8,
            "lora_alpha": 16,
            "lora_dropout": 0.05,
            "lora_target_modules": ["q_proj", "v_proj"],
            "torch_num_threads": 2,
            "seed": 1729,
        },
    }


def _payload_v2(tmp_path: Path) -> dict[str, object]:
    payload = _payload(tmp_path)
    payload["schema_version"] = 2
    payload["scale_plan"] = {
        "plan_id": "physical-scale",
        "tiers": [
            {
                "tier_id": "pilot",
                "max_training_records": 10,
                "max_training_bytes": 4096,
                "max_validation_records": 10,
                "max_validation_bytes": 4096,
                "max_steps": 2,
            },
            {
                "tier_id": "small",
                "max_training_records": 100,
                "max_training_bytes": 65536,
                "max_validation_records": 20,
                "max_validation_bytes": 8192,
                "max_steps": 8,
            },
        ],
    }
    return payload


def _write_minimal_pe(path: Path) -> None:
    payload = bytearray(132)
    payload[:2] = b"MZ"
    payload[60:64] = (128).to_bytes(4, "little")
    payload[128:132] = b"PE\0\0"
    path.write_bytes(payload)


def _config(tmp_path: Path) -> driver.PhysicalPilotConfig:
    raw = json.dumps(_payload(tmp_path), ensure_ascii=False, sort_keys=True)
    return driver.PhysicalPilotConfig.from_json(raw)


def test_material_totals_reject_worker_record_overflow() -> None:
    materials = SimpleNamespace(
        evidence=SimpleNamespace(
            materials=(
                SimpleNamespace(
                    split=driver.LearningDataSplit.TRAINING,
                    record_count=driver._TRAINER_MAX_RECORDS,
                    byte_count=1,
                ),
                SimpleNamespace(
                    split=driver.LearningDataSplit.VALIDATION,
                    record_count=1,
                    byte_count=1,
                ),
            )
        )
    )

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="canonical PEFT record limit",
    ):
        driver._material_totals(materials)


def test_bounded_reader_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "bounded.bin"
    path.write_bytes(b"x" * 9)

    with pytest.raises(driver.PhysicalPilotDriverError, match="size is invalid"):
        driver._read_bounded_file(path, max_bytes=8, name="test input")


def test_bounded_reader_accepts_exact_limit(tmp_path: Path) -> None:
    path = tmp_path / "bounded.bin"
    path.write_bytes(b"x" * 8)

    assert driver._read_bounded_file(path, max_bytes=8, name="test input") == b"x" * 8


def test_config_file_rejects_oversized_bytes(tmp_path: Path) -> None:
    path = tmp_path / "physical-pilot.json"
    path.write_bytes(b"x" * (driver._CONFIG_MAX_BYTES + 1))

    with pytest.raises(driver.PhysicalPilotDriverError, match="config size is invalid"):
        driver._read_config(path)


def test_config_parses_exact_runtime_and_resource_authority(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert dict(config.runtime_versions) == _payload(tmp_path)["runtime_versions"]
    assert config.resource_budget.max_cpu_percent == 95.0
    assert config.resource_budget.max_memory_percent == 90.0
    assert config.trainer_parameters.lora_target_modules == ("q_proj", "v_proj")
    assert config.output_root == tmp_path / "pilot-output"


def test_config_v1_preserves_legacy_implicit_scale_plan(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.scale_plan is None


def test_config_v2_accepts_canonical_multi_tier_scale_plan(tmp_path: Path) -> None:
    payload = _payload_v2(tmp_path)
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))

    assert config.scale_plan is not None
    assert config.scale_plan.plan_id == "physical-scale"
    assert tuple(tier.tier_id for tier in config.scale_plan.tiers) == (
        "pilot",
        "small",
    )
    assert tuple(tier.max_steps for tier in config.scale_plan.tiers) == (2, 8)


def test_config_v2_rejects_non_monotonic_scale_plan(tmp_path: Path) -> None:
    payload = _payload_v2(tmp_path)
    plan = payload["scale_plan"]
    assert isinstance(plan, dict)
    tiers = plan["tiers"]
    assert isinstance(tiers, list)
    second = tiers[1]
    assert isinstance(second, dict)
    second["max_training_records"] = 1

    with pytest.raises(driver.PhysicalPilotDriverError, match="scale_plan is invalid"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_v2_requires_exact_scale_plan_fields(tmp_path: Path) -> None:
    payload = _payload_v2(tmp_path)
    plan = payload["scale_plan"]
    assert isinstance(plan, dict)
    plan["unexpected"] = True

    with pytest.raises(driver.PhysicalPilotDriverError, match="scale_plan fields"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


@pytest.mark.parametrize("schema_version", (True, 1.0, "1"))
def test_config_rejects_non_integer_schema_version(
    tmp_path: Path,
    schema_version: object,
) -> None:
    payload = _payload(tmp_path)
    payload["schema_version"] = schema_version

    with pytest.raises(driver.PhysicalPilotDriverError, match="unsupported"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_v2_requires_scale_plan(tmp_path: Path) -> None:
    payload = _payload_v2(tmp_path)
    del payload["scale_plan"]

    with pytest.raises(driver.PhysicalPilotDriverError, match="fields are invalid"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_configured_multi_tier_plan_binds_frozen_evaluation_set(tmp_path: Path) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v2(tmp_path)))

    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )

    assert plan.evaluation_set_sha256 == "e" * 64
    assert plan.plan_id == "physical-scale"
    assert tuple(tier.tier_id for tier in plan.tiers) == ("pilot", "small")
    assert plan.tiers[0].max_steps == 2
    assert plan.tiers[1].max_steps == 8


def test_legacy_scale_plan_uses_observed_pilot_bounds(tmp_path: Path) -> None:
    config = _config(tmp_path)

    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )

    assert plan.plan_id == "physical-pilot"
    assert len(plan.tiers) == 1
    assert plan.tiers[0].tier_id == "pilot"
    assert plan.tiers[0].max_training_records == 3
    assert plan.tiers[0].max_training_bytes == 1024
    assert plan.tiers[0].max_validation_records == 2
    assert plan.tiers[0].max_validation_bytes == 512
    assert plan.tiers[0].max_steps == 2


def test_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["unexpected"] = True

    with pytest.raises(driver.PhysicalPilotDriverError, match="fields are invalid"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_rejects_identifier_beyond_runtime_bound(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["job_id"] = "j" * 513

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="incompatible with TrainingJobSpec",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("base_artifact_ref", "C:\\private\\base.gguf"),
        ("base_artifact_ref", "/private/base.gguf"),
        ("candidate_artifact_ref", "\\private\\candidate"),
        ("candidate_artifact_ref", "file:///C:/private/candidate"),
    ),
)
def test_config_rejects_private_local_artifact_reference(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _payload(tmp_path)
    payload[field] = value

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="public logical artifact reference",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_rejects_candidate_equal_to_base(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["candidate_artifact_ref"] = payload["base_artifact_ref"]

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="incompatible with TrainingJobSpec",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_rejects_missing_runtime_distribution(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    runtime_versions = payload["runtime_versions"]
    assert isinstance(runtime_versions, dict)
    del runtime_versions["gguf"]

    with pytest.raises(driver.PhysicalPilotDriverError, match="exactly"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_rejects_duplicate_target_module(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    trainer_parameters = payload["trainer_parameters"]
    assert isinstance(trainer_parameters, dict)
    trainer_parameters["lora_target_modules"] = ["q_proj", "q_proj"]

    with pytest.raises(driver.PhysicalPilotDriverError, match="duplicates"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "target",
    ("q proj", "модуль", "q,proj", "q\\proj"),
)
def test_config_rejects_worker_incompatible_target_module(
    tmp_path: Path,
    target: str,
) -> None:
    payload = _payload(tmp_path)
    trainer_parameters = payload["trainer_parameters"]
    assert isinstance(trainer_parameters, dict)
    trainer_parameters["lora_target_modules"] = [target]

    with pytest.raises(driver.PhysicalPilotDriverError, match="trainer token grammar"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_rejects_nonpublic_candidate_reference(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    descriptor = payload["candidate_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor["source_reference"] = "https://example.com/model?private=1"

    with pytest.raises(driver.PhysicalPilotDriverError, match="provenance is invalid"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    (
        b"MZ",
        b"not-a-pe",
        b"MZ" + b"\0" * 58 + (17_000_000).to_bytes(4, "little"),
    ),
)
def test_invalid_trainer_pe_fails_before_durable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: True)
    config.blob_store_root.mkdir()
    config.frozen_package_path.write_text("{}", encoding="utf-8")
    config.trainer_executable.write_bytes(payload)
    config.base_gguf_path.write_bytes(b"GGUF")
    config.model_dir.mkdir()
    (config.model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    with pytest.raises(driver.PhysicalPilotDriverError, match="Windows PE"):
        driver.run_physical_pilot_from_config(config)

    assert not config.output_root.exists()


def test_non_windows_gate_precedes_filesystem_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: False)

    with pytest.raises(driver.PhysicalPilotDriverError, match="must execute on Windows"):
        driver.run_physical_pilot_from_config(config)

    assert not config.output_root.exists()


def test_invalid_model_directory_fails_before_durable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: True)
    config.blob_store_root.mkdir()
    config.frozen_package_path.write_text("{}", encoding="utf-8")
    _write_minimal_pe(config.trainer_executable)
    config.base_gguf_path.write_bytes(b"GGUF")
    config.model_dir.mkdir()

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="model_dir is not a canonical local model directory",
    ):
        driver.run_physical_pilot_from_config(config)

    assert not config.output_root.exists()


@pytest.mark.parametrize("authority", ("model", "blobs"))
def test_output_root_cannot_mutate_input_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    payload = _payload(tmp_path)
    payload["output_root"] = str(tmp_path / authority / "pilot-output")
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))
    monkeypatch.setattr(driver, "_is_windows", lambda: True)
    config.blob_store_root.mkdir()
    config.frozen_package_path.write_text("{}", encoding="utf-8")
    _write_minimal_pe(config.trainer_executable)
    config.base_gguf_path.write_bytes(b"GGUF")
    config.model_dir.mkdir()
    (config.model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="inside an input authority directory",
    ):
        driver.run_physical_pilot_from_config(config)

    assert not config.output_root.exists()



def test_candidate_descriptor_uses_only_completed_digest_and_materialized_size(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    candidate_path = tmp_path / "adapter_model.safetensors"
    payload = b"candidate-adapter-bytes"
    candidate_path.write_bytes(payload)
    completed = SimpleNamespace(candidate_sha256="a" * 64)

    descriptor = driver._candidate_descriptor(
        config=config,
        candidate_path=candidate_path,
        completed=completed,
    )

    assert descriptor.sha256 == "a" * 64
    assert descriptor.model_version == "a" * 64
    assert descriptor.size_bytes == len(payload)
    assert descriptor.model_id == config.candidate_descriptor.model_id
    assert descriptor.source_reference == config.candidate_descriptor.source_reference
    assert descriptor.license_reference == config.candidate_descriptor.license_reference


def test_candidate_descriptor_rejects_missing_completed_digest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate_path = tmp_path / "adapter_model.safetensors"
    candidate_path.write_bytes(b"candidate-adapter-bytes")

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="missing candidate digest",
    ):
        driver._candidate_descriptor(
            config=config,
            candidate_path=candidate_path,
            completed=SimpleNamespace(candidate_sha256=None),
        )

def test_duplicate_json_field_is_rejected(tmp_path: Path) -> None:
    payload = json.dumps(_payload(tmp_path))
    duplicate = payload[:-1] + ',"job_id":"other"}'

    with pytest.raises(driver.PhysicalPilotDriverError, match="invalid JSON"):
        driver.PhysicalPilotConfig.from_json(duplicate)
