from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningShard,
)
from nika_core.runtime.idempotency import IdempotencyLedger

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


def test_candidate_training_digests_require_cross_tier_evidence(
    proof: ModuleType,
) -> None:
    previous = _sha(b"previous")
    trained = _sha(b"trained")
    tokenization = _sha(b"tokenization")

    assert proof._candidate_training_digests(
        {
            "previous_adapter_tensors_sha256": previous,
            "trained_adapter_tensors_sha256": trained,
            "tokenization_sha256": tokenization,
        },
        name="candidate",
    ) == (previous, trained, tokenization)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"tokenization_sha256": None}, "tokenization_sha256"),
        ({"tokenization_sha256": "A" * 64}, "tokenization_sha256"),
        (
            {
                "previous_adapter_tensors_sha256": _sha(b"same"),
                "trained_adapter_tensors_sha256": _sha(b"same"),
            },
            "does not prove adapter tensor mutation",
        ),
    ],
)
def test_candidate_training_digests_fail_closed(
    proof: ModuleType,
    patch: dict[str, object],
    match: str,
) -> None:
    manifest: dict[str, object] = {
        "previous_adapter_tensors_sha256": _sha(b"previous"),
        "trained_adapter_tensors_sha256": _sha(b"trained"),
        "tokenization_sha256": _sha(b"tokenization"),
    }
    manifest.update(patch)

    with pytest.raises(proof.ProofError, match=match):
        proof._candidate_training_digests(manifest, name="candidate")


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
        **{
            key: value
            for key, value in tiers[0].items()
            if key not in {"max_steps", "tier_id"}
        },
        "max_steps": 3,
        "tier_id": "scale-1",
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



def test_read_object_rejects_duplicate_and_nonfinite_json(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":2}',
        encoding="utf-8",
    )
    with pytest.raises(proof.ProofError, match="duplicate JSON key"):
        proof._read_object(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(proof.ProofError, match="non-finite JSON constant"):
        proof._read_object(nonfinite)


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

    with pytest.raises(ValueError, match="trusted learning-package digest mismatch"):
        proof.prepare_tier0(tmp_path)


def test_prepare_tier1_binds_promoted_candidate_as_logical_base(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package()
    (tmp_path / "frozen-package.json").write_text(
        package.to_json(),
        encoding="utf-8",
    )
    tier0_config = {
        "schema_version": 2,
        "workspace_id": "physical-proof-workspace",
        "frozen_package_sha256": package.manifest_sha256,
        "base_artifact_ref": "models/base",
        "output_root": str(tmp_path / "run"),
        "candidate_artifact_ref": "models/tier0-adapter",
        "scale_plan": {
            "plan_id": "physical-real-scale-progression",
            "tiers": [
                {"tier_id": "pilot", "max_steps": 2},
                {"tier_id": "scale-1", "max_steps": 3},
            ],
        },
    }
    (tmp_path / "physical-pilot.json").write_text(
        json.dumps(tier0_config),
        encoding="utf-8",
    )
    tier0_root = tmp_path / "run"
    tier0_root.mkdir()
    candidate = tmp_path / "tier0-adapter.safetensors"
    candidate.write_bytes(b"promoted-adapter")
    candidate_sha256 = _sha(candidate.read_bytes())
    report = SimpleNamespace(
        candidate_artifact_ref="models/tier0-adapter",
        candidate_sha256=candidate_sha256,
        candidate_byte_count=candidate.stat().st_size,
    )
    (tier0_root / "physical-old-new-evaluation-report.json").write_text(
        json.dumps(
            {
                "experiment_status": "promoted",
                "selected_candidate_id": report.candidate_artifact_ref,
            }
        ),
        encoding="utf-8",
    )
    claim = {
        "plan_sha256": _sha(b"plan"),
        "tier_index": 0,
        "candidate_artifact_ref": report.candidate_artifact_ref,
        "candidate_sha256": report.candidate_sha256,
    }
    progression = SimpleNamespace(
        tier_index=0,
        candidate_artifact_ref=report.candidate_artifact_ref,
        candidate_sha256=report.candidate_sha256,
        canonical_payload=lambda: claim,
    )
    monkeypatch.setattr(proof, "_pilot_report", lambda _root: report)
    monkeypatch.setattr(
        proof,
        "_trusted_progression",
        lambda _root, workspace_id: progression,
    )
    monkeypatch.setattr(
        proof,
        "candidate_artifact_path",
        lambda _root, _ref: candidate,
    )

    proof.prepare_tier1(tmp_path)

    tier1_package_path = tmp_path / "tier1-frozen-package.json"
    tier1_package = FrozenLearningPackage.from_json(
        tier1_package_path.read_bytes()
    )
    assert tier1_package.base_artifact_sha256 == candidate_sha256
    assert tier1_package.evaluation_set_sha256 == package.evaluation_set_sha256
    tier1 = json.loads(
        (tmp_path / "tier1-physical-pilot.json").read_text(
            encoding="utf-8"
        )
    )
    assert tier1["schema_version"] == 3
    assert tier1["base_artifact_ref"] == report.candidate_artifact_ref
    assert tier1["frozen_package_sha256"] == tier1_package.manifest_sha256
    assert tier1["scale_tier_id"] == "scale-1"
    assert tier1["progression_proof"] == claim
    assert Path(tier1["initial_adapter_path"]) == candidate.resolve()
    assert tier1["candidate_artifact_ref"] == (
        "models/nika-physical-scale-tier1-adapter"
    )


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

def _complete_scale_progression_record(
    store: SQLiteStore,
    *,
    task_id: str,
    operation_key: str,
    claim: dict[str, object],
    operation_type: str = "training.physical_scale_progression",
) -> None:
    ledger = IdempotencyLedger(store)
    record, created = ledger.reserve_once(
        operation_key=operation_key,
        task_id=task_id,
        operation_type=operation_type,
        input_fingerprint="sha256:" + _sha(operation_key.encode("utf-8")),
    )
    assert created is True
    ledger.complete_pending_if_matches(
        operation_key=record.operation_key,
        task_id=record.task_id,
        operation_type=record.operation_type,
        input_fingerprint=record.input_fingerprint,
        created_at=record.created_at,
        result={
            "schema": "nika-physical-scale-progression-record-v1",
            "proof_sha256": _sha(
                json.dumps(
                    claim,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ),
            "proof": claim,
        },
    )


def _progression_discovery_fixture(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SQLiteStore, object, dict[str, object]]:
    root = (tmp_path / "progression-run").resolve()
    root.mkdir()
    store = SQLiteStore(root / "physical-pilot.sqlite3")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="physical-proof-workspace",
        agent_id="physical-peft-pilot",
        payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
    )
    claim = {
        "plan_sha256": _sha(b"plan"),
        "tier_index": 0,
        "candidate_artifact_ref": "models/tier0-adapter",
        "candidate_sha256": _sha(b"candidate"),
    }
    _complete_scale_progression_record(
        store,
        task_id=task.task_id,
        operation_key="scale-progression-one",
        claim=claim,
    )
    report = SimpleNamespace(job_id="pilot-job")
    restored = SimpleNamespace(canonical_payload=lambda: dict(claim))
    monkeypatch.setattr(proof, "_pilot_report", lambda _root: report)
    monkeypatch.setattr(
        proof,
        "load_trusted_scale_progression_proof",
        lambda _root, *, workspace_id, expected_claim: (
            restored
            if workspace_id == "physical-proof-workspace"
            and expected_claim == claim
            else (_ for _ in ()).throw(AssertionError("unexpected trusted restore"))
        ),
    )
    return root, store, task, claim


def test_trusted_progression_discovery_streams_durable_history(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _store, _task, claim = _progression_discovery_fixture(
        proof,
        tmp_path,
        monkeypatch,
    )

    def forbid_list_for_task(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("qualification must stream durable ledger rows")

    monkeypatch.setattr(
        proof.IdempotencyLedger,
        "list_for_task",
        forbid_list_for_task,
    )

    restored = proof._trusted_progression(
        root,
        workspace_id="physical-proof-workspace",
    )

    assert restored.canonical_payload() == claim


def test_trusted_progression_discovery_ignores_other_completed_operations(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, task, claim = _progression_discovery_fixture(
        proof,
        tmp_path,
        monkeypatch,
    )
    _complete_scale_progression_record(
        store,
        task_id=task.task_id,
        operation_key="unrelated-lookalike",
        operation_type="training.irrelevant",
        claim=claim,
    )

    restored = proof._trusted_progression(
        root,
        workspace_id="physical-proof-workspace",
    )

    assert restored.canonical_payload() == claim


def test_trusted_progression_discovery_rejects_duplicate_durable_claims(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, task, claim = _progression_discovery_fixture(
        proof,
        tmp_path,
        monkeypatch,
    )
    _complete_scale_progression_record(
        store,
        task_id=task.task_id,
        operation_key="scale-progression-two",
        claim=claim,
    )
    monkeypatch.setattr(
        proof,
        "load_trusted_scale_progression_proof",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate discovery must fail before trusted restore")
        ),
    )

    with pytest.raises(
        proof.ProofError,
        match="exactly one durable scale progression proof is required",
    ):
        proof._trusted_progression(
            root,
            workspace_id="physical-proof-workspace",
        )

