from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_proof_stable_file_bytes_round_trip(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"evidence-bytes")

    assert proof._stable_file_bytes(
        target,
        max_bytes=1024,
        name="evidence",
    ) == b"evidence-bytes"


def test_proof_stable_file_bytes_rejects_bound(
    proof: ModuleType,
    tmp_path: Path,
) -> None:
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"evidence-bytes")

    with pytest.raises(proof.ProofError, match="size or file type is invalid"):
        proof._stable_file_bytes(target, max_bytes=4, name="evidence")


def test_proof_stable_file_bytes_rejects_post_read_identity_change(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"evidence-bytes")
    original_lstat = proof.os.lstat
    initial = original_lstat(target)
    calls = 0

    def changed_lstat(path: object):
        nonlocal calls
        value = original_lstat(path)
        if Path(path) != target:
            return value
        calls += 1
        if calls == 1:
            return value
        return SimpleNamespace(
            st_mode=initial.st_mode,
            st_dev=initial.st_dev,
            st_ino=initial.st_ino,
            st_size=initial.st_size,
            st_mtime_ns=initial.st_mtime_ns + 1,
            st_file_attributes=getattr(initial, "st_file_attributes", 0),
        )

    monkeypatch.setattr(proof.os, "lstat", changed_lstat)

    with pytest.raises(
        proof.ProofError,
        match="changed while it was being snapshotted",
    ):
        proof._stable_file_bytes(target, max_bytes=1024, name="evidence")


def _staged_asset_manifest(
    proof: ModuleType,
    *,
    config_body: bytes,
    gguf_body: bytes,
) -> bytes:
    payload = {
        "license": proof._MODEL_LICENSE,
        "license_reference": proof._MODEL_LICENSE_REFERENCE,
        "model_files": [
            {
                "path": "config.json",
                "sha256": _sha(config_body),
                "size_bytes": len(config_body),
            },
            {
                "path": proof._GGUF_FILE,
                "sha256": _sha(gguf_body),
                "size_bytes": len(gguf_body),
            },
        ],
        "repository": proof._MODEL_REPOSITORY,
        "revision": proof._MODEL_REVISION,
        "runtime_versions": {"test-runtime": "1.0"},
        "source_reference": proof._MODEL_SOURCE_REFERENCE,
    }
    return (proof._canonical_json(payload) + "\n").encode("utf-8")


def test_proof_staged_assets_bind_exact_pilot_model_authority(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    config_body = b'{"model_type":"test"}'
    gguf_body = b"gguf-bytes"
    (model_dir / "config.json").write_bytes(config_body)
    (tmp_path / "base.gguf").write_bytes(gguf_body)
    model_manifest = _sha(b"model-dir-manifest")

    monkeypatch.setattr(proof, "_MODEL_FILES", ("config.json",))
    monkeypatch.setattr(proof, "_RUNTIME_PACKAGES", ("test-runtime",))
    monkeypatch.setattr(proof, "_runtime_versions", lambda: {"test-runtime": "1.0"})
    monkeypatch.setattr(
        proof,
        "model_directory_manifest_sha256",
        lambda path: model_manifest,
    )
    pilot = SimpleNamespace(
        model_dir_manifest_sha256=model_manifest,
        base_sha256=_sha(gguf_body),
    )
    raw = _staged_asset_manifest(
        proof,
        config_body=config_body,
        gguf_body=gguf_body,
    )

    verified = proof._verified_staged_assets(tmp_path, raw, pilot=pilot)

    assert verified["repository"] == proof._MODEL_REPOSITORY
    assert verified["revision"] == proof._MODEL_REVISION


def test_proof_staged_assets_reject_post_pilot_model_change(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    config_body = b'{"model_type":"test"}'
    gguf_body = b"gguf-bytes"
    config = model_dir / "config.json"
    config.write_bytes(config_body)
    (tmp_path / "base.gguf").write_bytes(gguf_body)
    model_manifest = _sha(b"model-dir-manifest")

    monkeypatch.setattr(proof, "_MODEL_FILES", ("config.json",))
    monkeypatch.setattr(proof, "_RUNTIME_PACKAGES", ("test-runtime",))
    monkeypatch.setattr(proof, "_runtime_versions", lambda: {"test-runtime": "1.0"})
    monkeypatch.setattr(
        proof,
        "model_directory_manifest_sha256",
        lambda path: model_manifest,
    )
    pilot = SimpleNamespace(
        model_dir_manifest_sha256=model_manifest,
        base_sha256=_sha(gguf_body),
    )
    raw = _staged_asset_manifest(
        proof,
        config_body=config_body,
        gguf_body=gguf_body,
    )
    config.write_bytes(b'{"model_type":"changed"}')

    with pytest.raises(proof.ProofError, match="staged asset identity changed"):
        proof._verified_staged_assets(tmp_path, raw, pilot=pilot)


def test_proof_pilot_config_rejects_alias_path(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    base = tmp_path / "base.gguf"
    base.write_bytes(b"base")
    output = tmp_path / "run"
    output.mkdir()
    digest = _sha(b"frozen")
    pilot = SimpleNamespace(
        candidate_artifact_ref="candidate-ref",
        frozen_package_sha256=digest,
    )
    monkeypatch.setattr(proof, "_RUNTIME_PACKAGES", ("test-runtime",))
    monkeypatch.setattr(proof, "_runtime_versions", lambda: {"test-runtime": "1.0"})
    payload = {
        "schema_version": 1,
        "base_artifact_ref": proof._BASE_ARTIFACT_REF,
        "candidate_artifact_ref": "candidate-ref",
        "frozen_package_sha256": digest,
        "runtime_versions": {"test-runtime": "1.0"},
        "model_dir": str(model_dir / ".." / "model"),
        "base_gguf_path": str(base),
        "output_root": str(output),
    }

    with pytest.raises(proof.ProofError, match="non-canonical model_dir"):
        proof._verified_pilot_config(
            tmp_path,
            (proof._canonical_json(payload) + "\n").encode("utf-8"),
            pilot=pilot,
        )


def _evaluation_report_fixture(proof: ModuleType) -> dict[str, object]:
    digest = _sha(b"digest")
    return {
        "schema_version": 2,
        "schema": "nika-physical-old-new-evaluation-report-v2",
        "physical_pilot_evidence_sha256": digest,
        "requested_experiment_id": proof._EXPERIMENT_ID,
        "evaluation_set_sha256": digest,
        "execution_config_sha256": digest,
        "comparison_evidence_sha256": digest,
        "experiment_id": "effect-id",
        "experiment_status": "completed",
        "selected_candidate_id": "candidate-ref",
        "previous_champion_id": "base-ref",
        "training_binding_sha256": digest,
        "champion_binding_sha256": digest,
        "champion_benchmark_sha256": digest,
        "challenger_benchmark_sha256": digest,
        "attestor_id": "attestor",
        "attestor_sha256": digest,
        "champion_provider_manifest_sha256": None,
        "challenger_provider_manifest_sha256": None,
        "definition_sha256": digest,
        "observations_sha256": digest,
        "observation_count": 2,
    }


def test_proof_rejects_inconsistent_comparison_evidence(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluation_report_fixture(proof)
    digest = str(report["physical_pilot_evidence_sha256"])
    pilot = SimpleNamespace(
        schema_version=6,
        platform="windows",
        completed_steps=2,
        evidence_sha256=digest,
        candidate_artifact_ref="candidate-ref",
    )
    monkeypatch.setattr(
        proof,
        "_comparison_evidence_sha256_from_report",
        lambda value: _sha(b"different-comparison"),
    )

    with pytest.raises(proof.ProofError, match="digest is inconsistent"):
        proof._verified_evaluation_report(
            report,
            pilot=pilot,
            evaluation_set_sha256=digest,
            previous_champion_id="base-ref",
        )


def test_proof_rejects_wrong_previous_champion_authority(
    proof: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluation_report_fixture(proof)
    digest = str(report["physical_pilot_evidence_sha256"])
    pilot = SimpleNamespace(
        schema_version=6,
        platform="windows",
        completed_steps=2,
        evidence_sha256=digest,
        candidate_artifact_ref="candidate-ref",
    )
    monkeypatch.setattr(
        proof,
        "_comparison_evidence_sha256_from_report",
        lambda value: digest,
    )

    with pytest.raises(proof.ProofError, match="wrong previous_champion_id"):
        proof._verified_evaluation_report(
            report,
            pilot=pilot,
            evaluation_set_sha256=digest,
            previous_champion_id="different-base",
        )


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

def _candidate_manifest_fixture(
    *,
    tokenization_sha256: object,
    candidate_artifact_ref: str = "candidate-ref",
    previous_adapter_tensors_sha256: str | None = None,
    trained_adapter_tensors_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema": "nika-peft-candidate-v2",
        "candidate_artifact_ref": candidate_artifact_ref,
        "previous_adapter_tensors_sha256": previous_adapter_tensors_sha256,
        "trained_adapter_tensors_sha256": (
            trained_adapter_tensors_sha256 or _sha(b"trained-tensors")
        ),
        "tokenization_sha256": tokenization_sha256,
    }


def test_candidate_tokenization_snapshot_binds_exact_bytes(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_bytes = b"exact-candidate-snapshot"
    tokenization_sha256 = _sha(b"canonical-tokenization")
    pilot = SimpleNamespace(
        candidate_artifact_ref="candidate-ref",
        previous_adapter_tensors_sha256=None,
        trained_adapter_tensors_sha256=_sha(b"trained-tensors"),
    )
    observed: dict[str, object] = {}

    def fake_manifest(path: Path) -> dict[str, object]:
        observed["path"] = path
        observed["bytes"] = Path(path).read_bytes()
        return _candidate_manifest_fixture(
            tokenization_sha256=tokenization_sha256,
        )

    monkeypatch.setattr(proof, "candidate_adapter_manifest", fake_manifest)

    result = proof._verified_candidate_tokenization_from_snapshot(
        tmp_path.resolve(),
        candidate_bytes,
        pilot=pilot,
    )

    assert result == tokenization_sha256
    assert observed["bytes"] == candidate_bytes
    snapshot_path = observed["path"]
    assert isinstance(snapshot_path, Path)
    assert not snapshot_path.exists()


@pytest.mark.parametrize(
    "tokenization_sha256",
    [None, 7, "", "A" * 64, "0" * 63],
)
def test_candidate_tokenization_snapshot_requires_canonical_digest(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokenization_sha256: object,
) -> None:
    pilot = SimpleNamespace(
        candidate_artifact_ref="candidate-ref",
        previous_adapter_tensors_sha256=None,
        trained_adapter_tensors_sha256=_sha(b"trained-tensors"),
    )
    monkeypatch.setattr(
        proof,
        "candidate_adapter_manifest",
        lambda path: _candidate_manifest_fixture(
            tokenization_sha256=tokenization_sha256,
        ),
    )

    with pytest.raises(
        proof.ProofError,
        match="lacks canonical tokenization evidence",
    ):
        proof._verified_candidate_tokenization_from_snapshot(
            tmp_path.resolve(),
            b"candidate-bytes",
            pilot=pilot,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "nika-peft-candidate-v3", "candidate-v2"),
        ("candidate_artifact_ref", "different", "logical reference changed"),
        (
            "previous_adapter_tensors_sha256",
            _sha(b"unexpected-previous"),
            "previous tensor digest",
        ),
        (
            "trained_adapter_tensors_sha256",
            _sha(b"unexpected-trained"),
            "trained tensor digest",
        ),
    ],
)
def test_candidate_tokenization_snapshot_binds_pilot_tensor_identity(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    pilot = SimpleNamespace(
        candidate_artifact_ref="candidate-ref",
        previous_adapter_tensors_sha256=None,
        trained_adapter_tensors_sha256=_sha(b"trained-tensors"),
    )
    manifest = _candidate_manifest_fixture(
        tokenization_sha256=_sha(b"canonical-tokenization"),
    )
    manifest[field] = value
    monkeypatch.setattr(
        proof,
        "candidate_adapter_manifest",
        lambda path: dict(manifest),
    )

    with pytest.raises(proof.ProofError, match=message):
        proof._verified_candidate_tokenization_from_snapshot(
            tmp_path.resolve(),
            b"candidate-bytes",
            pilot=pilot,
        )


def test_candidate_tokenization_snapshot_fails_closed_if_manifest_path_mutates(
    proof: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_bytes = b"exact-candidate-snapshot"
    pilot = SimpleNamespace(
        candidate_artifact_ref="candidate-ref",
        previous_adapter_tensors_sha256=None,
        trained_adapter_tensors_sha256=_sha(b"trained-tensors"),
    )

    def mutating_manifest(path: Path) -> dict[str, object]:
        Path(path).write_bytes(b"mutated-after-snapshot")
        return _candidate_manifest_fixture(
            tokenization_sha256=_sha(b"unbound-tokenization"),
        )

    monkeypatch.setattr(proof, "candidate_adapter_manifest", mutating_manifest)

    with pytest.raises(proof.ProofError):
        proof._verified_candidate_tokenization_from_snapshot(
            tmp_path.resolve(),
            candidate_bytes,
            pilot=pilot,
        )

