from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import ModelCandidate
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import AttestedTrainingCandidateGateway
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_champion import (
    ChampionEvaluationBinding,
    ChampionEvaluationBindingError,
    bind_champion_for_attested_evaluation,
)
from nika_core.training_evaluation_subprocess import RegistrySubprocessLoadedModelAttestor


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_HELD_OUT_SHA256 = _sha(b"held-out")
_ATTESTOR_SCRIPT_NAME = "champion-evaluator.py"


def _champion_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    ModelArtifactDescriptor,
    ModelCandidate,
    TrainingEvaluationBinding,
]:
    body = b"exact-pre-training-champion-bytes\n"
    path = tmp_path / "base model.bin"
    path.write_bytes(body)
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="base-model",
        model_version="base-v1",
        source_reference="model:base-v1",
        license_reference="license:base-v1",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_sha(body),
        size_bytes=len(body),
    )
    champion = ModelCandidate(
        candidate_id="models/base",
        provider_id=descriptor.provider_id,
        provider_kind=ProviderKind.LOCAL,
        request_model=descriptor.model_id,
        expected_response_model=descriptor.model_id,
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref=descriptor.source_reference,
        model_license_ref=descriptor.license_reference,
        model_sha256=descriptor.sha256,
    )
    training_binding = TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id=champion.candidate_id,
        base_provider_id=champion.provider_id,
        base_model_id=champion.request_model,
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id="ollama",
        challenger_model_id="candidate-model",
        base_sha256=descriptor.sha256,
        challenger_sha256=_sha(b"trained-candidate"),
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"frozen-package"),
        evaluation_set_sha256=_HELD_OUT_SHA256,
        base_descriptor_digest=descriptor.descriptor_digest,
        base_descriptor_registry_key=descriptor.registry_key,
        base_size_bytes=descriptor.size_bytes,
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry"),
        challenger_size_bytes=len(b"trained-candidate"),
    )
    return path, descriptor, champion, training_binding


def _bind(
    tmp_path: Path,
    *,
    descriptor: ModelArtifactDescriptor | None = None,
    champion: ModelCandidate | None = None,
    training_binding: TrainingEvaluationBinding | None = None,
) -> ChampionEvaluationBinding:
    path, default_descriptor, default_champion, default_training = _champion_fixture(
        tmp_path
    )
    return bind_champion_for_attested_evaluation(
        training_binding=training_binding or default_training,
        champion=champion or default_champion,
        descriptor=descriptor or default_descriptor,
        champion_path=path,
        allowed_root=tmp_path,
    )


def _request(binding: ChampionEvaluationBinding) -> ModelRequest:
    return ModelRequest(
        request_id="champion-benchmark-request-1",
        messages=(ModelMessage(role="user", content="question"),),
        model=binding.model_id,
        provider_id=binding.provider_id,
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
        metadata={
            "model_candidate_id": binding.candidate_id,
            "evaluation_set_sha256": binding.evaluation_set_sha256,
        },
    )


def _success_script(tmp_path: Path) -> Path:
    path = tmp_path / _ATTESTOR_SCRIPT_NAME
    path.write_text(
        """
import hashlib
import json
import sys
from pathlib import Path

request = json.loads(sys.stdin.buffer.read())
candidate = Path(request["candidate"]["path"]).read_bytes()
response = {
    "protocol_version": request["protocol_version"],
    "request_id": request["request"]["request_id"],
    "provider_id": request["request"]["provider_id"],
    "model": request["request"]["model"],
    "text": "answer",
    "loaded_artifact_sha256": hashlib.sha256(candidate).hexdigest(),
    "loaded_artifact_size_bytes": len(candidate),
    "descriptor_digest": request["binding"]["descriptor_digest"],
    "usage": {
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    },
}
sys.stdout.write(json.dumps(response))
""".strip(),
        encoding="utf-8",
    )
    return path


def _concrete_attestor(
    tmp_path: Path,
    binding: ChampionEvaluationBinding,
    descriptor: ModelArtifactDescriptor,
    candidate_path: Path,
) -> RegistrySubprocessLoadedModelAttestor:
    del binding
    script = _success_script(tmp_path)
    executable = Path(sys.executable).resolve()
    roots = tuple(dict.fromkeys((executable.parent, tmp_path.resolve())))
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "champion-evaluation-artifacts.sqlite3"),
        local_file_roots=roots,
    )
    executable_record = registry.register_file(
        workspace_id="champion-evaluation-tests",
        idempotency_key="python-evaluator-executable",
        path=executable,
        kind="model_evaluator_executable",
    )
    script_record = registry.register_file(
        workspace_id="champion-evaluation-tests",
        idempotency_key="champion-evaluator-script",
        path=script,
        kind="model_evaluator_command_file",
    )
    return RegistrySubprocessLoadedModelAttestor(
        (str(executable), str(script)),
        artifact_registry=registry,
        evaluator_artifact_id=executable_record.artifact_id,
        command_artifact_ids={1: script_record.artifact_id},
        candidate_path=str(candidate_path.resolve()),
        descriptor=descriptor,
        allowed_root=str(tmp_path.resolve()),
        timeout_seconds=5.0,
    )


def test_champion_binding_physically_binds_training_base_and_held_out_set(
    tmp_path: Path,
) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)

    binding = bind_champion_for_attested_evaluation(
        training_binding=training,
        champion=champion,
        descriptor=descriptor,
        champion_path=path,
        allowed_root=tmp_path,
    )

    assert binding.candidate_id == training.base_candidate_id
    assert binding.artifact_sha256 == training.base_sha256
    assert binding.evaluation_set_sha256 == training.evaluation_set_sha256
    assert binding.training_binding_sha256 == training.binding_sha256
    assert binding.descriptor_digest == descriptor.descriptor_digest
    assert binding.descriptor_registry_key == descriptor.registry_key
    assert binding.challenger_candidate_id == binding.candidate_id
    assert binding.challenger_provider_id == binding.provider_id
    assert binding.challenger_model_id == binding.model_id
    assert binding.challenger_sha256 == binding.artifact_sha256
    assert binding.challenger_size_bytes == binding.artifact_size_bytes
    assert len(binding.binding_sha256) == 64


def test_champion_binding_evidence_is_secret_free_and_path_free(tmp_path: Path) -> None:
    binding = _bind(tmp_path)

    body = json.dumps(binding.evidence_payload(), ensure_ascii=False)

    assert str(tmp_path) not in body
    assert "base model.bin" not in body
    assert set(binding.evidence_payload()) == {
        "schema",
        "job_id",
        "candidate_id",
        "provider_id",
        "model_id",
        "artifact_sha256",
        "artifact_size_bytes",
        "frozen_package_sha256",
        "evaluation_set_sha256",
        "descriptor_digest",
        "descriptor_registry_key",
        "training_binding_sha256",
    }


def test_champion_artifact_tamper_fails_physical_binding(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    path.write_bytes(b"substituted-base-model")

    with pytest.raises(
        ChampionEvaluationBindingError,
        match="physical artifact verification failed",
    ):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=champion,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_digest_must_equal_training_base(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    substituted = replace(champion, model_sha256=_sha(b"other-base"))

    with pytest.raises(ChampionEvaluationBindingError, match="training base artifact"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=substituted,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_identity_must_equal_training_base_candidate(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    substituted = replace(champion, candidate_id="models/other-base")

    with pytest.raises(ChampionEvaluationBindingError, match="training base candidate"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=substituted,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_provider_model_route_must_match_descriptor(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    substituted = replace(champion, request_model="other-model")

    with pytest.raises(ChampionEvaluationBindingError, match="provider/model route"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=substituted,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_provenance_must_match_descriptor(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    substituted = replace(champion, model_license_ref="license:other")

    with pytest.raises(ChampionEvaluationBindingError, match="provenance"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=substituted,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_requires_local_sha256_descriptor(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    cloud = replace(
        descriptor,
        kind=ModelArtifactKind.CLOUD,
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        sha256=None,
        size_bytes=None,
    )

    with pytest.raises(ChampionEvaluationBindingError, match="local model artifact"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=champion,
            descriptor=cloud,
            champion_path=path,
            allowed_root=tmp_path,
        )


def test_champion_path_must_remain_inside_allowed_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-base.bin"
    body = b"outside-base-model"
    outside.write_bytes(body)
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="base-model",
        source_reference="model:outside-base",
        license_reference="license:outside-base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_sha(body),
        size_bytes=len(body),
    )
    champion = ModelCandidate(
        candidate_id="models/base",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="base-model",
        expected_response_model="base-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref=descriptor.source_reference,
        model_license_ref=descriptor.license_reference,
        model_sha256=descriptor.sha256,
    )
    _, _, _, training = _champion_fixture(tmp_path)
    training = replace(
        training,
        base_sha256=descriptor.sha256,
        base_descriptor_digest=descriptor.descriptor_digest,
        base_descriptor_registry_key=descriptor.registry_key,
        base_size_bytes=descriptor.size_bytes,
    )

    try:
        with pytest.raises(
            ChampionEvaluationBindingError,
            match="physical artifact verification failed",
        ):
            bind_champion_for_attested_evaluation(
                training_binding=training,
                champion=champion,
                descriptor=descriptor,
                champion_path=outside,
                allowed_root=tmp_path,
            )
    finally:
        outside.unlink(missing_ok=True)


def test_direct_champion_binding_construction_is_disabled() -> None:
    with pytest.raises(TypeError):
        ChampionEvaluationBinding(
            job_id="job",
            candidate_id="candidate",
            provider_id="provider",
            model_id="model",
            artifact_sha256="a" * 64,
            artifact_size_bytes=1,
            frozen_package_sha256="b" * 64,
            evaluation_set_sha256="c" * 64,
            descriptor_digest="d" * 64,
            descriptor_registry_key="e" * 64,
            training_binding_sha256="f" * 64,
        )


@pytest.mark.asyncio
async def test_incumbent_attested_gateway_accepts_champion_binding(
    tmp_path: Path,
) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    binding = bind_champion_for_attested_evaluation(
        training_binding=training,
        champion=champion,
        descriptor=descriptor,
        champion_path=path,
        allowed_root=tmp_path,
    )
    attestor = _concrete_attestor(tmp_path, binding, descriptor, path)
    gateway = AttestedTrainingCandidateGateway(
        attestor,
        binding=binding,  # type: ignore[arg-type]
        expected_attestor_id=attestor.attestor_id,
        expected_attestor_sha256=attestor.attestor_sha256,
    )

    response = await gateway.complete(_request(binding))

    assert response.text == "answer"
    assert response.provider_id == binding.provider_id
    assert response.model == binding.model_id


@pytest.mark.asyncio
async def test_wrong_held_out_identity_stops_before_champion_subprocess_effect(
    tmp_path: Path,
) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    binding = bind_champion_for_attested_evaluation(
        training_binding=training,
        champion=champion,
        descriptor=descriptor,
        champion_path=path,
        allowed_root=tmp_path,
    )
    attestor = _concrete_attestor(tmp_path, binding, descriptor, path)
    gateway = AttestedTrainingCandidateGateway(
        attestor,
        binding=binding,  # type: ignore[arg-type]
        expected_attestor_id=attestor.attestor_id,
        expected_attestor_sha256=attestor.attestor_sha256,
    )
    wrong = replace(
        _request(binding),
        metadata={
            "model_candidate_id": binding.candidate_id,
            "evaluation_set_sha256": _sha(b"other-held-out"),
        },
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(wrong)

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert "evaluation-set identity" in str(exc_info.value)


def test_mutated_training_binding_cannot_retarget_champion(tmp_path: Path) -> None:
    path, descriptor, champion, training = _champion_fixture(tmp_path)
    object.__setattr__(training, "base_sha256", _sha(b"mutated-base"))

    with pytest.raises(ChampionEvaluationBindingError, match="training base artifact"):
        bind_champion_for_attested_evaluation(
            training_binding=training,
            champion=champion,
            descriptor=descriptor,
            champion_path=path,
            allowed_root=tmp_path,
        )
