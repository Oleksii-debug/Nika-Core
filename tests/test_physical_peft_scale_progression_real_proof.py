from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningShard,
)

_ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = _ROOT / "scripts" / "physical_peft_scale_progression_real_proof.py"
    spec = importlib.util.spec_from_file_location(
        "physical_peft_scale_progression_real_proof_test",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proof() -> ModuleType:
    return _load_script()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _package() -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="proof",
        package_version="1",
        base_artifact_sha256=_sha(b"base"),
        selection_policy_sha256=_sha(b"selection"),
        verification_sha256=_sha(b"verification"),
        evaluation_set_sha256=_sha(b"evaluation"),
        shards=(
            LearningShard(
                split=LearningDataSplit.TRAINING,
                artifact_sha256=_sha(b"train"),
                provenance_sha256=_sha(b"train-provenance"),
                license_evidence_sha256=_sha(b"train-license"),
                record_count=4,
                byte_count=80,
            ),
            LearningShard(
                split=LearningDataSplit.VALIDATION,
                artifact_sha256=_sha(b"validation"),
                provenance_sha256=_sha(b"validation-provenance"),
                license_evidence_sha256=_sha(b"validation-license"),
                record_count=1,
                byte_count=24,
            ),
        ),
    )


def test_scale_plan_expands_only_full_step_budget(
    proof: ModuleType,
) -> None:
    payload = proof._scale_plan_payload(_package())

    assert payload["plan_id"] == "physical-real-scale-progression"
    tiers = payload["tiers"]
    assert len(tiers) == 2
    assert tiers[0] == {
        "max_training_records": 4,
        "max_training_bytes": 80,
        "max_validation_records": 1,
        "max_validation_bytes": 24,
        "max_steps": 2,
        "tier_id": "pilot",
    }
    assert tiers[1] == {
        **{key: value for key, value in tiers[0].items() if key != "max_steps"},
        "max_steps": 3,
    }


def test_prepare_tier0_upgrades_canonical_preparation_once(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    package = _package()
    package_path = tmp_path / "frozen-package.json"
    package_path.write_text(package.to_json(), encoding="utf-8")
    config_path = tmp_path / "physical-pilot.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "frozen_package_sha256": package.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )

    proof.prepare_tier0(tmp_path)

    value = json.loads(config_path.read_text(encoding="utf-8"))
    assert value["schema_version"] == 2
    assert value["scale_plan"] == proof._scale_plan_payload(package)

    with pytest.raises(
        proof.ProofError,
        match="must start from canonical schema-v1",
    ):
        proof.prepare_tier0(tmp_path)


def test_prepare_tier0_rejects_wrong_frozen_package_digest(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    package = _package()
    (tmp_path / "frozen-package.json").write_text(
        package.to_json(),
        encoding="utf-8",
    )
    (tmp_path / "physical-pilot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "frozen_package_sha256": _sha(b"wrong"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="trusted learning-package digest mismatch"):
        proof.prepare_tier0(tmp_path)


def test_configure_promotion_uses_explicit_non_regression_policy(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "physical-evaluation.json"
    config_path.write_text(
        json.dumps(
            {
                "experiment_id": "old-proof",
                "policy": {
                    "primary_metric": "model_quality_score",
                    "minimum_improvement": 0.000001,
                    "minimum_replays": 1,
                    "primary_higher_is_better": True,
                    "guardrails": [
                        {
                            "metric": "model_task_pass",
                            "higher_is_better": True,
                            "max_regression": 1.0,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    proof.configure_promotion(tmp_path)

    value = json.loads(config_path.read_text(encoding="utf-8"))
    assert value["experiment_id"] == proof._EXPERIMENT_ID
    assert value["policy"]["minimum_improvement"] == 0.0
    assert value["policy"]["guardrails"][0]["max_regression"] == 1.0


@pytest.mark.parametrize(
    ("policy_patch", "match"),
    [
        ({"primary_metric": "wrong"}, "canonical real-proof policy"),
        ({"minimum_replays": 2}, "canonical real-proof policy"),
        ({"primary_higher_is_better": False}, "canonical real-proof policy"),
    ],
)
def test_configure_promotion_rejects_noncanonical_policy(
    proof: ModuleType,
    tmp_path: Path,
    policy_patch: dict[str, object],
    match: str,
) -> None:
    policy = {
        "primary_metric": "model_quality_score",
        "minimum_improvement": 0.000001,
        "minimum_replays": 1,
        "primary_higher_is_better": True,
        "guardrails": [],
    }
    policy.update(policy_patch)
    (tmp_path / "physical-evaluation.json").write_text(
        json.dumps({"experiment_id": "old", "policy": policy}),
        encoding="utf-8",
    )

    with pytest.raises(proof.ProofError, match=match):
        proof.configure_promotion(tmp_path)
