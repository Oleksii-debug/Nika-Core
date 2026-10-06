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
from nika_core.training_physical_evaluation_driver import _evaluation_set_from_json

_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative: str) -> ModuleType:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evaluator() -> ModuleType:
    return _load_script(
        "physical_peft_real_evaluator_test",
        "scripts/physical_peft_real_evaluator.py",
    )


@pytest.fixture
def proof() -> ModuleType:
    return _load_script(
        "physical_peft_evaluation_real_proof_test",
        "scripts/physical_peft_evaluation_real_proof.py",
    )


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _request(candidate_path: Path, candidate_body: bytes) -> bytes:
    digest = _sha(candidate_body)
    payload = {
        "protocol_version": 1,
        "request": {
            "request_id": "request-1",
            "messages": [{"role": "user", "content": "held-out"}],
            "model": "model-1",
            "provider_id": "provider-1",
            "provider_kind": "local",
            "privacy": "private",
            "timeout_seconds": 30.0,
            "temperature": 0.0,
            "metadata": {},
        },
        "binding": {
            "binding_sha256": _sha(b"binding"),
            "challenger_candidate_id": "candidate-1",
            "artifact_sha256": digest,
            "artifact_size_bytes": len(candidate_body),
            "descriptor_digest": _sha(b"descriptor"),
            "descriptor_registry_key": _sha(b"registry"),
        },
        "candidate": {
            "path": str(candidate_path),
            "sha256": digest,
            "size_bytes": len(candidate_body),
        },
        "attestor": {
            "attestor_id": "attestor-1",
            "attestor_sha256": _sha(b"attestor"),
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def test_evaluator_protocol_accepts_exact_attested_candidate(
    evaluator: ModuleType,
    tmp_path: Path,
) -> None:
    candidate_body = b"candidate-bytes"
    candidate = tmp_path / "candidate.safetensors"
    candidate.write_bytes(candidate_body)

    parsed = evaluator._validated_request(_request(candidate, candidate_body))

    assert parsed["candidate"]["sha256"] == _sha(candidate_body)
    assert parsed["binding"]["artifact_sha256"] == _sha(candidate_body)


def test_evaluator_protocol_rejects_binding_candidate_disagreement(
    evaluator: ModuleType,
    tmp_path: Path,
) -> None:
    candidate_body = b"candidate-bytes"
    candidate = tmp_path / "candidate.safetensors"
    candidate.write_bytes(candidate_body)
    payload = json.loads(_request(candidate, candidate_body))
    payload["binding"]["artifact_sha256"] = _sha(b"different")

    with pytest.raises(evaluator.EvaluatorError, match="digest disagrees"):
        evaluator._validated_request(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )


def test_evaluator_snapshots_exact_model_bytes_before_load(
    evaluator: ModuleType,
    tmp_path: Path,
) -> None:
    body = b"exact-model-bytes"
    source = (tmp_path / "base.gguf").resolve()
    source.write_bytes(body)
    destination = tmp_path / "snapshot.gguf"

    evaluator._snapshot_artifact(
        source,
        destination,
        expected_sha256=_sha(body),
        expected_size=len(body),
    )

    assert destination.read_bytes() == body


def test_evaluator_snapshot_rejects_wrong_digest_without_residue(
    evaluator: ModuleType,
    tmp_path: Path,
) -> None:
    body = b"exact-model-bytes"
    source = (tmp_path / "base.gguf").resolve()
    source.write_bytes(body)
    destination = tmp_path / "snapshot.gguf"

    with pytest.raises(evaluator.EvaluatorError, match="do not match"):
        evaluator._snapshot_artifact(
            source,
            destination,
            expected_sha256=_sha(b"wrong"),
            expected_size=len(body),
        )

    assert not destination.exists()


def test_proof_evaluation_json_reconstructs_same_content_identity(
    proof: ModuleType,
) -> None:
    evaluation, payload = proof._evaluation_set()

    reconstructed = _evaluation_set_from_json(proof._canonical_json(payload))

    assert reconstructed.content_sha256 == evaluation.content_sha256
    assert reconstructed.purpose.value == "held_out"


def test_prepare_replaces_placeholder_with_real_held_out_identity(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    training = LearningShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha(b"training"),
        provenance_sha256=_sha(b"training-provenance"),
        license_evidence_sha256=_sha(b"training-license"),
        record_count=1,
        byte_count=8,
    )
    validation = LearningShard(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha(b"validation"),
        provenance_sha256=_sha(b"validation-provenance"),
        license_evidence_sha256=_sha(b"validation-license"),
        record_count=1,
        byte_count=10,
    )
    package = FrozenLearningPackage.freeze(
        package_id="proof",
        package_version="1",
        base_artifact_sha256=_sha(b"base"),
        selection_policy_sha256=_sha(b"selection"),
        verification_sha256=_sha(b"verification"),
        evaluation_set_sha256=_sha(b"placeholder"),
        shards=(training, validation),
    )
    package_path = (tmp_path / "frozen-package.json").resolve()
    package_path.write_text(package.to_json(), encoding="utf-8")
    config_path = tmp_path / "physical-pilot.json"
    config_path.write_text(
        json.dumps(
            {
                "frozen_package_path": str(package_path),
                "frozen_package_sha256": package.manifest_sha256,
            }
        ),
        encoding="utf-8",
    )

    proof.prepare(tmp_path)

    replacement = FrozenLearningPackage.from_json(package_path.read_bytes())
    evaluation, _ = proof._evaluation_set()
    updated_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert replacement.evaluation_set_sha256 == evaluation.content_sha256
    assert updated_config["frozen_package_sha256"] == replacement.manifest_sha256
    assert (tmp_path / "held-out-evaluation.json").is_file()
