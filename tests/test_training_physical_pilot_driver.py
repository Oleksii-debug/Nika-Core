from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.training_physical_pilot_driver as driver
import nika_core.training_scale as scale


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


def _progression_payload(
    *,
    plan_sha256: str = "a" * 64,
    candidate_sha256: str = "b" * 64,
    evaluation_set_sha256: str = "e" * 64,
) -> dict[str, object]:
    return {
        "authorization_sha256": "1" * 64,
        "base_artifact_ref": "models/base",
        "base_sha256": "2" * 64,
        "candidate_artifact_ref": "models/pilot-candidate",
        "candidate_sha256": candidate_sha256,
        "comparison_evidence_sha256": "3" * 64,
        "evaluation_set_sha256": evaluation_set_sha256,
        "execution_plan_sha256": "4" * 64,
        "frozen_package_sha256": "5" * 64,
        "job_fingerprint": "6" * 64,
        "job_id": "pilot-job",
        "plan_sha256": plan_sha256,
        "tier_index": 0,
        "training_material_sha256": "7" * 64,
    }


def _payload_v3(tmp_path: Path) -> dict[str, object]:
    payload = _payload_v2(tmp_path)
    payload["schema_version"] = 3
    payload["job_id"] = "small-job"
    payload["base_artifact_ref"] = "models/pilot-candidate"
    payload["candidate_artifact_ref"] = "models/small-candidate"
    payload["initial_adapter_path"] = str(tmp_path / "promoted.safetensors")
    payload["scale_tier_id"] = "small"
    payload["progression_proof"] = _progression_payload()
    return payload


def _trusted_progression_proof(
    payload: dict[str, object],
) -> scale.TrainingScaleProgressionProof:
    return scale._build_progression_proof(
        plan_sha256=payload["plan_sha256"],
        tier_index=payload["tier_index"],
        authorization_sha256=payload["authorization_sha256"],
        job_id=payload["job_id"],
        job_fingerprint=payload["job_fingerprint"],
        base_artifact_ref=payload["base_artifact_ref"],
        base_sha256=payload["base_sha256"],
        candidate_artifact_ref=payload["candidate_artifact_ref"],
        candidate_sha256=payload["candidate_sha256"],
        frozen_package_sha256=payload["frozen_package_sha256"],
        training_material_sha256=payload["training_material_sha256"],
        execution_plan_sha256=payload["execution_plan_sha256"],
        comparison_evidence_sha256=payload["comparison_evidence_sha256"],
        evaluation_set_sha256=payload["evaluation_set_sha256"],
    )

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


def test_bounded_reader_rejects_linked_authority_file(tmp_path: Path) -> None:
    target = tmp_path / "authority.bin"
    target.write_bytes(b"trusted")
    path = tmp_path / "authority-link.bin"
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this runner")

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="canonical non-linked regular file",
    ):
        driver._read_bounded_file(path, max_bytes=32, name="test input")


def test_bounded_reader_rejects_path_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authority.bin"
    path.write_bytes(b"trusted")
    real_lstat = driver.os.lstat
    matching_calls = 0

    def changing_lstat(target: object) -> object:
        nonlocal matching_calls
        value = real_lstat(target)
        if Path(target) == path:
            matching_calls += 1
            if matching_calls == 2:
                path.write_bytes(b"replacement")
                value = real_lstat(target)
        return value

    monkeypatch.setattr(driver.os, "lstat", changing_lstat)

    with pytest.raises(driver.PhysicalPilotDriverError, match="changed while"):
        driver._read_bounded_file(path, max_bytes=32, name="test input")


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_bounded_reader_refuses_preexisting_writer(tmp_path: Path) -> None:
    path = tmp_path / "authority.bin"
    path.write_bytes(b"trusted")

    with path.open("r+b"):
        with pytest.raises(
            driver.PhysicalPilotDriverError,
            match="could not be read",
        ):
            driver._read_bounded_file(path, max_bytes=32, name="test input")


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_bounded_reader_releases_share_fence_after_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "authority.bin"
    path.write_bytes(b"trusted")

    assert driver._read_bounded_file(path, max_bytes=32, name="test input") == b"trusted"

    path.write_bytes(b"replacement")
    assert path.read_bytes() == b"replacement"


def test_config_file_rejects_oversized_bytes(tmp_path: Path) -> None:
    path = tmp_path / "physical-pilot.json"
    path.write_bytes(b"x" * (driver._CONFIG_MAX_BYTES + 1))

    with pytest.raises(driver.PhysicalPilotDriverError, match="config size is invalid"):
        driver._read_config(path)


def test_higher_tier_cli_requires_durable_progression_root(
    tmp_path: Path,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(
        json.dumps(_payload_v3(tmp_path))
    )

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="--trusted-progression-root",
    ):
        driver._trusted_progression_for_cli(config, source_root=None)


def test_pilot_tier_cli_rejects_irrelevant_progression_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="pilot-tier execution",
    ):
        driver._trusted_progression_for_cli(
            config,
            source_root=tmp_path,
        )


def test_higher_tier_cli_loads_exact_durable_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload_v3(tmp_path)
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))
    root = (tmp_path / "previous-run").resolve()
    root.mkdir()
    claim = config.progression_proof_payload
    assert claim is not None
    trusted = _trusted_progression_proof(claim)
    observed: dict[str, object] = {}

    def load(
        value: Path,
        *,
        workspace_id: str,
        expected_claim: dict[str, object],
    ) -> driver.TrainingScaleProgressionProof:
        observed.update(
            root=value,
            workspace_id=workspace_id,
            expected_claim=expected_claim,
        )
        return trusted

    monkeypatch.setattr(driver, "load_trusted_scale_progression_proof", load)

    restored = driver._trusted_progression_for_cli(
        config,
        source_root=root,
    )

    assert restored is trusted
    assert observed == {
        "root": root,
        "workspace_id": config.workspace_id,
        "expected_claim": claim,
    }


def test_main_passes_durable_progression_authority_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = (tmp_path / "physical-pilot.json").resolve()
    config_path.write_text("{}", encoding="utf-8")
    root = (tmp_path / "previous-run").resolve()
    root.mkdir()
    config = driver.PhysicalPilotConfig.from_json(
        json.dumps(_payload_v3(tmp_path))
    )
    claim = config.progression_proof_payload
    assert claim is not None
    trusted = _trusted_progression_proof(claim)
    monkeypatch.setattr(driver, "_read_config", lambda _: config)
    monkeypatch.setattr(
        driver,
        "_trusted_progression_for_cli",
        lambda _config, *, source_root: trusted,
    )
    observed: dict[str, object] = {}

    def run(
        value: driver.PhysicalPilotConfig,
        *,
        trusted_progression_proof: driver.TrainingScaleProgressionProof | None,
    ) -> SimpleNamespace:
        observed["config"] = value
        observed["proof"] = trusted_progression_proof
        return SimpleNamespace(to_json=lambda: '{"ok":true}')

    monkeypatch.setattr(driver, "run_physical_pilot_from_config", run)

    status = driver.main(
        [
            str(config_path),
            "--trusted-progression-root",
            str(root),
        ]
    )

    assert status == 0
    assert observed == {"config": config, "proof": trusted}
    assert capsys.readouterr().out.strip() == '{"ok":true}'


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


def test_physical_step_budget_preserves_pilot_and_scales_higher_tier(
    tmp_path: Path,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v2(tmp_path)))
    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )

    assert driver._physical_training_max_steps(
        tier_index=0,
        tier=plan.tiers[0],
    ) == 2
    assert driver._physical_training_max_steps(
        tier_index=1,
        tier=plan.tiers[1],
    ) == 8


def test_physical_step_budget_rejects_oversized_tier_before_execution() -> None:
    tier = driver.TrainingScaleTier(
        tier_id="oversized",
        max_training_records=100,
        max_training_bytes=65536,
        max_validation_records=20,
        max_validation_bytes=8192,
        max_steps=driver.PHYSICAL_TRAINING_MAX_STEPS + 1,
    )

    assert driver._physical_training_max_steps(
        tier_index=0,
        tier=tier,
    ) == 2
    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="max_steps exceeds the physical execution ceiling",
    ):
        driver._physical_training_max_steps(
            tier_index=1,
            tier=tier,
        )


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


def test_config_v3_parses_progression_claim_without_granting_authority(
    tmp_path: Path,
) -> None:
    payload = _payload_v3(tmp_path)
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))

    assert config.scale_plan is not None
    assert config.scale_tier_id == "small"
    assert config.initial_adapter_path == tmp_path / "promoted.safetensors"
    assert config.progression_proof_payload == payload["progression_proof"]
    assert config.progression_proof_payload["tier_index"] == 0
    assert (
        config.progression_proof_payload["candidate_artifact_ref"]
        == config.base_artifact_ref
    )


def test_config_v3_rejects_first_tier_as_progression_target(tmp_path: Path) -> None:
    payload = _payload_v3(tmp_path)
    payload["scale_tier_id"] = "pilot"

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="declared higher tier",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_v3_rejects_skipped_progression_proof(tmp_path: Path) -> None:
    payload = _payload_v3(tmp_path)
    proof = payload["progression_proof"]
    assert isinstance(proof, dict)
    proof["tier_index"] = 1

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="previous scale tier",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_config_v3_rejects_promoted_reference_mismatch(tmp_path: Path) -> None:
    payload = _payload_v3(tmp_path)
    proof = payload["progression_proof"]
    assert isinstance(proof, dict)
    proof["candidate_artifact_ref"] = "models/other-candidate"

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="promoted candidate",
    ):
        driver.PhysicalPilotConfig.from_json(json.dumps(payload))


def test_physical_training_task_payload_persists_canonical_scale_plan(
    tmp_path: Path,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v2(tmp_path)))
    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )

    payload = driver._physical_training_task_payload(
        job_id="pilot-job",
        plan=plan,
        tier_index=0,
        progression_proof=None,
    )

    assert payload["kind"] == "physical_peft_pilot"
    assert payload["scale_tier_id"] == "pilot"
    assert payload["scale_plan_sha256"] == plan.plan_sha256
    assert payload["scale_plan"] == plan.canonical_payload()
    assert payload["progression_proof"] is None
    assert payload["progression_proof_sha256"] is None


def test_higher_tier_task_payload_persists_exact_trusted_predecessor(
    tmp_path: Path,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v3(tmp_path)))
    assert config.scale_plan is not None
    plan = driver.TrainingScalePlan(
        plan_id=config.scale_plan.plan_id,
        evaluation_set_sha256="e" * 64,
        tiers=config.scale_plan.tiers,
    )
    claim = dict(config.progression_proof_payload or {})
    claim["plan_sha256"] = plan.plan_sha256
    claim["evaluation_set_sha256"] = plan.evaluation_set_sha256
    trusted = _trusted_progression_proof(claim)

    payload = driver._physical_training_task_payload(
        job_id="small-job",
        plan=plan,
        tier_index=1,
        progression_proof=trusted,
    )

    assert payload["kind"] == "physical_peft_scale_tier"
    assert payload["scale_tier_id"] == "small"
    assert payload["progression_proof"] == trusted.canonical_payload()
    assert payload["progression_proof_sha256"] == trusted.proof_sha256


def test_higher_tier_preflight_binds_plan_package_and_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_adapter = tmp_path / "promoted.safetensors"
    initial_adapter.write_bytes(b"promoted-adapter")
    adapter_sha256 = driver._stable_file_sha256(
        initial_adapter,
        name="test promoted adapter",
    )
    preliminary = driver.PhysicalPilotConfig.from_json(
        json.dumps(_payload_v3(tmp_path))
    )
    plan = driver._scale_plan_for_physical_pilot(
        preliminary,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )
    payload = _payload_v3(tmp_path)
    payload["progression_proof"] = _progression_payload(
        plan_sha256=plan.plan_sha256,
        candidate_sha256=adapter_sha256,
        evaluation_set_sha256=plan.evaluation_set_sha256,
    )
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))
    monkeypatch.setattr(
        driver,
        "candidate_adapter_manifest",
        lambda _: {"candidate_artifact_ref": config.base_artifact_ref},
    )

    tier_index, tier = driver._selected_scale_tier(config, plan)
    claim = config.progression_proof_payload
    assert claim is not None
    trusted = _trusted_progression_proof(claim)
    proof = driver._preflight_higher_tier(
        config,
        trusted_progression_proof=trusted,
        plan=plan,
        tier_index=tier_index,
        initial_adapter_path=initial_adapter,
        material_base_sha256=adapter_sha256,
    )

    assert tier_index == 1
    assert tier.tier_id == "small"
    assert proof is trusted
    assert proof.proof_sha256 == trusted.proof_sha256


def test_higher_tier_preflight_rejects_adapter_package_digest_mismatch(
    tmp_path: Path,
) -> None:
    initial_adapter = tmp_path / "promoted.safetensors"
    initial_adapter.write_bytes(b"promoted-adapter")
    preliminary = driver.PhysicalPilotConfig.from_json(
        json.dumps(_payload_v3(tmp_path))
    )
    plan = driver._scale_plan_for_physical_pilot(
        preliminary,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )
    payload = _payload_v3(tmp_path)
    payload["progression_proof"] = _progression_payload(
        plan_sha256=plan.plan_sha256,
        candidate_sha256="d" * 64,
        evaluation_set_sha256=plan.evaluation_set_sha256,
    )
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))
    tier_index, _ = driver._selected_scale_tier(config, plan)
    claim = config.progression_proof_payload
    assert claim is not None
    trusted = _trusted_progression_proof(claim)

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="initial_adapter_path does not match",
    ):
        driver._preflight_higher_tier(
            config,
            trusted_progression_proof=trusted,
            plan=plan,
            tier_index=tier_index,
            initial_adapter_path=initial_adapter,
            material_base_sha256="d" * 64,
        )


def test_higher_tier_preflight_rejects_missing_trusted_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v3(tmp_path)))
    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )
    tier_index, _ = driver._selected_scale_tier(config, plan)
    adapter = tmp_path / "promoted.safetensors"
    adapter.write_bytes(b"must-not-be-read")

    monkeypatch.setattr(
        driver,
        "_stable_file_sha256",
        lambda *_args, **_kwargs: pytest.fail("adapter read preceded trust gate"),
    )

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="independently trusted progression authority",
    ):
        driver._preflight_higher_tier(
            config,
            trusted_progression_proof=None,
            plan=plan,
            tier_index=tier_index,
            initial_adapter_path=adapter,
            material_base_sha256="b" * 64,
        )


def test_higher_tier_preflight_rejects_claim_trusted_authority_mismatch(
    tmp_path: Path,
) -> None:
    payload = _payload_v3(tmp_path)
    config = driver.PhysicalPilotConfig.from_json(json.dumps(payload))
    plan = driver._scale_plan_for_physical_pilot(
        config,
        evaluation_set_sha256="e" * 64,
        training_records=3,
        training_bytes=1024,
        validation_records=2,
        validation_bytes=512,
    )
    tier_index, _ = driver._selected_scale_tier(config, plan)
    claim = config.progression_proof_payload
    assert claim is not None
    trusted_payload = dict(claim)
    trusted_payload["comparison_evidence_sha256"] = "f" * 64
    trusted = _trusted_progression_proof(trusted_payload)
    adapter = tmp_path / "promoted.safetensors"
    adapter.write_bytes(b"must-not-be-read")

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="does not match trusted progression authority",
    ):
        driver._preflight_higher_tier(
            config,
            trusted_progression_proof=trusted,
            plan=plan,
            tier_index=tier_index,
            initial_adapter_path=adapter,
            material_base_sha256="b" * 64,
        )


def test_higher_tier_run_fails_closed_without_trusted_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = driver.PhysicalPilotConfig.from_json(json.dumps(_payload_v3(tmp_path)))
    monkeypatch.setattr(
        driver,
        "_is_windows",
        lambda: pytest.fail("platform check preceded trust gate"),
    )

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="independently trusted progression authority",
    ):
        driver.run_physical_pilot_from_config(config)


def test_stable_file_sha256_rejects_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.safetensors"
    path.write_bytes(b"candidate")
    real_lstat = driver.os.lstat
    calls = 0

    def changing_lstat(target: Path) -> object:
        nonlocal calls
        value = real_lstat(target)
        calls += 1
        if calls == 2:
            path.write_bytes(b"changed-candidate")
            value = real_lstat(target)
        return value

    monkeypatch.setattr(driver.os, "lstat", changing_lstat)

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="changed while",
    ):
        driver._stable_file_sha256(path, name="candidate")


def test_stable_file_sha256_rejects_linked_authority_file(tmp_path: Path) -> None:
    target = tmp_path / "candidate.safetensors"
    target.write_bytes(b"trusted")
    path = tmp_path / "candidate-link.safetensors"
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this runner")

    with pytest.raises(
        driver.PhysicalPilotDriverError,
        match="canonical non-linked regular file",
    ):
        driver._stable_file_sha256(path, name="initial_adapter_path")


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_stable_file_sha256_refuses_preexisting_writer(tmp_path: Path) -> None:
    path = tmp_path / "candidate.safetensors"
    path.write_bytes(b"trusted")

    with path.open("r+b"):
        with pytest.raises(
            driver.PhysicalPilotDriverError,
            match="could not be snapshotted",
        ):
            driver._stable_file_sha256(path, name="candidate")


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


def test_trainer_pe_reader_rejects_path_mutation_during_header_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trainer.exe"
    _write_minimal_pe(path)
    real_lstat = driver.os.lstat
    matching_calls = 0

    def changing_lstat(target: object) -> object:
        nonlocal matching_calls
        value = real_lstat(target)
        if Path(target) == path:
            matching_calls += 1
            if matching_calls == 2:
                path.write_bytes(b"replacement")
                value = real_lstat(target)
        return value

    monkeypatch.setattr(driver.os, "lstat", changing_lstat)

    with pytest.raises(driver.PhysicalPilotDriverError, match="changed while"):
        driver._require_windows_pe_executable(path)


@pytest.mark.skipif(driver.os.name != "nt", reason="Windows file-share semantics")
def test_trainer_pe_reader_refuses_preexisting_writer(tmp_path: Path) -> None:
    path = tmp_path / "trainer.exe"
    _write_minimal_pe(path)

    with path.open("r+b"):
        with pytest.raises(
            driver.PhysicalPilotDriverError,
            match="Windows PE header could not be read",
        ):
            driver._require_windows_pe_executable(path)


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
