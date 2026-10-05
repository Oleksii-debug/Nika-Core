from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_champion_evaluation import (
    ChampionEvaluationBinding,
    bind_champion_artifact_for_evaluation,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_execution import TrainingEvaluationExecutionError


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_BASE_BYTES = b"base"
_BASE_SHA256 = _sha(_BASE_BYTES)
_CHALLENGER_SHA256 = _sha(b"challenger")
_ATTESTOR_ID = "test-champion-attestor"
_ATTESTOR_SHA256 = _sha(b"champion-attestor")


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="loop-c-held-out",
        version="2026-10-05.v1",
        provenance_ref="dataset:loop-c-held-out",
        license_ref="license:internal-eval",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="question one"),),
                expected_text="answer",
            ),
            EvaluationCase(
                case_id="case-2",
                messages=(ModelMessage(role="user", content="question two"),),
                expected_text="answer",
            ),
        ),
    )


def _training_binding(evaluation_set: EvaluationSet) -> TrainingEvaluationBinding:
    return TrainingEvaluationBinding(
        job_id="training-job-1",
        base_candidate_id="models/base",
        challenger_candidate_id="models/challenger",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        base_sha256=_BASE_SHA256,
        challenger_sha256=_CHALLENGER_SHA256,
        candidate_artifact_ref="models/challenger",
        frozen_package_sha256=_sha(b"package"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry"),
        challenger_size_bytes=len(b"challenger"),
    )


def _champion() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="models/base",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="base-model",
        expected_response_model="base-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:base",
        model_license_ref="license:base",
        model_sha256=_BASE_SHA256,
    )


def _descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="base-model",
        source_reference="model:base",
        license_reference="license:base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_BASE_SHA256,
        size_bytes=len(_BASE_BYTES),
    )


def _bind(tmp_path, *, payload: bytes = _BASE_BYTES) -> ChampionEvaluationBinding:
    evaluation = _evaluation_set()
    path = tmp_path / "base-model.bin"
    path.write_bytes(payload)
    return bind_champion_artifact_for_evaluation(
        training_binding=_training_binding(evaluation),
        champion=_champion(),
        descriptor=_descriptor(),
        candidate_path=path,
        allowed_root=tmp_path,
    )


class _ChampionEffectPort:
    def __init__(self, *, mode: str = "valid", text: str = "answer") -> None:
        self.mode = mode
        self.text = text
        self.calls = 0

    async def complete_attested(
        self,
        request,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        self.calls += 1
        if self.mode == "provider-no-effect":
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "private provider detail must not escape",
                provider_id=binding.challenger_provider_id,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        if self.mode == "untyped-failure":
            raise RuntimeError("secret raw provider detail")
        attestation = LoadedModelArtifactAttestation(
            request_id=request.request_id,
            binding_sha256=binding.binding_sha256,
            provider_id=binding.challenger_provider_id,
            model_id=binding.challenger_model_id,
            artifact_sha256=binding.challenger_sha256,
            descriptor_digest=binding.descriptor_digest,
            attestor_id=_ATTESTOR_ID,
            attestor_sha256=_ATTESTOR_SHA256,
        )
        if self.mode == "wrong-artifact":
            attestation = replace(
                attestation,
                artifact_sha256=_sha(b"substituted-base"),
            )
        return AttestedModelCompletionResult(
            response=ModelResponse(
                request_id=request.request_id,
                text=self.text,
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model=binding.challenger_model_id,
                usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            ),
            attestation=attestation,
        )


async def _run(tmp_path, port: _ChampionEffectPort):
    evaluation = _evaluation_set()
    training_binding = _training_binding(evaluation)
    path = tmp_path / "base-model.bin"
    path.write_bytes(_BASE_BYTES)
    champion_binding = bind_champion_artifact_for_evaluation(
        training_binding=training_binding,
        champion=_champion(),
        descriptor=_descriptor(),
        candidate_path=path,
        allowed_root=tmp_path,
    )
    return await run_attested_champion_benchmark(
        training_binding=training_binding,
        champion_binding=champion_binding,
        champion=_champion(),
        evaluation_set=evaluation,
        effect_port=port,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )


def test_bind_champion_reverifies_exact_physical_base_bytes(tmp_path) -> None:
    binding = _bind(tmp_path)

    assert binding.artifact_sha256 == _BASE_SHA256
    assert binding.candidate_id == "models/base"
    assert len(binding.binding_sha256) == 64


def test_bind_champion_rejects_physical_base_substitution(tmp_path) -> None:
    with pytest.raises(ValueError, match="physical artifact verification failed"):
        _bind(tmp_path, payload=b"evil")


@pytest.mark.asyncio
async def test_attested_champion_benchmark_emits_per_case_loaded_byte_receipts(
    tmp_path,
) -> None:
    port = _ChampionEffectPort()

    result = await _run(tmp_path, port)

    assert port.calls == 2
    assert result.report.completion_rate == 1.0
    assert [receipt.case_id for receipt in result.case_receipts] == ["case-1", "case-2"]
    assert all(receipt.artifact_sha256 == _BASE_SHA256 for receipt in result.case_receipts)
    assert result.evidence_payload()["case_count"] == 2
    body = json.dumps(result.evidence_payload(), ensure_ascii=False)
    assert "question one" not in body
    assert '"answer"' not in body
    assert len(result.evidence_sha256) == 64


@pytest.mark.asyncio
async def test_wrong_loaded_champion_artifact_is_unknown_and_fail_fast(tmp_path) -> None:
    port = _ChampionEffectPort(mode="wrong-artifact")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(tmp_path, port)

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_champion_provider_no_effect_failure_stops_before_second_case(tmp_path) -> None:
    port = _ChampionEffectPort(mode="provider-no-effect")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(tmp_path, port)

    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 1
    assert "private provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_untyped_champion_provider_failure_is_unknown_and_secret_minimized(
    tmp_path,
) -> None:
    port = _ChampionEffectPort(mode="untyped-failure")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(tmp_path, port)

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert "secret raw provider detail" not in str(exc_info.value)
    assert port.calls == 1
