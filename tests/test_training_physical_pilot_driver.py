from __future__ import annotations

import json
from pathlib import Path

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


def _config(tmp_path: Path) -> driver.PhysicalPilotConfig:
    raw = json.dumps(_payload(tmp_path), ensure_ascii=False, sort_keys=True)
    return driver.PhysicalPilotConfig.from_json(raw)


def test_config_parses_exact_runtime_and_resource_authority(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert dict(config.runtime_versions) == _payload(tmp_path)["runtime_versions"]
    assert config.resource_budget.max_cpu_percent == 95.0
    assert config.resource_budget.max_memory_percent == 90.0
    assert config.trainer_parameters.lora_target_modules == ("q_proj", "v_proj")
    assert config.output_root == tmp_path / "pilot-output"


def test_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["unexpected"] = True

    with pytest.raises(driver.PhysicalPilotDriverError, match="fields are invalid"):
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


def test_config_rejects_nonpublic_candidate_reference(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    descriptor = payload["candidate_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor["source_reference"] = "https://example.com/model?private=1"

    with pytest.raises(driver.PhysicalPilotDriverError, match="provenance is invalid"):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_non_windows_gate_precedes_filesystem_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: False)

    with pytest.raises(driver.PhysicalPilotDriverError, match="must execute on Windows"):
        driver.run_physical_pilot_from_config(config)

    assert not config.output_root.exists()


def test_duplicate_json_field_is_rejected(tmp_path: Path) -> None:
    payload = json.dumps(_payload(tmp_path))
    duplicate = payload[:-1] + ',"job_id":"other"}'

    with pytest.raises(driver.PhysicalPilotDriverError, match="invalid JSON"):
        driver.PhysicalPilotConfig.from_json(duplicate)

def _report() -> driver.PhysicalTrainingPilotReport:
    return driver.PhysicalTrainingPilotReport(
        job_id="pilot-job",
        base_sha256="1" * 64,
        frozen_package_sha256="2" * 64,
        training_material_sha256="3" * 64,
        scale_authorization_sha256="4" * 64,
        execution_plan_sha256="5" * 64,
        job_fingerprint="6" * 64,
        paused_checkpoint_id="pause-checkpoint",
        restart_checkpoint_id="restart-checkpoint",
        completed_checkpoint_id="complete-checkpoint",
        candidate_artifact_ref="models/pilot-candidate",
        candidate_descriptor_sha256="7" * 64,
        candidate_registry_key="8" * 64,
        candidate_sha256="9" * 64,
        candidate_byte_count=123,
        completed_steps=2,
    )


def test_report_writer_publishes_complete_canonical_json(tmp_path: Path) -> None:
    path = tmp_path / "physical-pilot-report.json"
    report = _report()

    driver._write_report(path, report)

    assert path.read_text(encoding="utf-8") == report.to_json() + "\n"
    assert not tuple(tmp_path.glob(".physical-pilot-report.*.tmp"))


def test_report_writer_never_clobbers_existing_evidence(tmp_path: Path) -> None:
    path = tmp_path / "physical-pilot-report.json"
    path.write_bytes(b"existing-evidence")

    with pytest.raises(driver.PhysicalPilotDriverError, match="already exists"):
        driver._write_report(path, _report())

    assert path.read_bytes() == b"existing-evidence"
    assert not tuple(tmp_path.glob(".physical-pilot-report.*.tmp"))


def test_report_writer_cleans_temporary_after_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "physical-pilot-report.json"
    monkeypatch.setattr(driver, "_is_windows", lambda: False)

    def fail_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert source.parent == tmp_path
        assert destination == path
        assert follow_symlinks is False
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(driver.os, "link", fail_link)

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="could not be persisted",
    ):
        driver._write_report(path, _report())

    assert not path.exists()
    assert not tuple(tmp_path.glob(".physical-pilot-report.*.tmp"))

