from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

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
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_champion import (
    AttestedChampionBenchmarkResult,
    AttestedOldNewEvaluationResult,
    ChampionEvaluationBinding,
    bind_champion_for_attested_evaluation,
    pair_attested_old_new_evaluation,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_execution import (
    AttestedChallengerBenchmarkResult,
    run_attested_challenger_benchmark,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_ATTESTOR_ID = "test-loaded-model-attestor"
_ATTESTOR_SHA256 = _sha(b"attestor-code")


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="loop-c-old-new-held-out",
        version="2026-10-05.v1",
        provenance_ref="dataset:loop-c-old-new-held-out",
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


def _descriptor(
    *,
    model_id: str,
    body: bytes,
    source_reference: str,
) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id=model_id,
        model_version="v1",
        source_reference=source_reference,
        license_reference=f"license:{model_id}",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_sha(body),
        size_bytes=len(body),
    )


def _candidate(
    *,
    candidate_id: str,
    descriptor: ModelArtifactDescriptor,
) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
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


def _authorities(
    tmp_path: Path,
) -> tuple[
    EvaluationSet,
    TrainingEvaluationBinding,
    ChampionEvaluationBinding,
    ModelCandidate,
    ModelCandidate,
]:
    evaluation_set = _evaluation_set()
    base_body = b"exact-old-champion-model\n"
    challenger_body = b"exact-trained-challenger-model\n"
    base_path = tmp_path / "base model.bin"
    base_path.write_bytes(base_body)

    base_descriptor = _descriptor(
        model_id="base-model",
        body=base_body,
        source_reference="model:base-v1",
    )
    challenger_descriptor = _descriptor(
        model_id="challenger-model",
        body=challenger_body,
        source_reference="model:challenger-v1",
    )
    champion = _candidate(
        candidate_id="models/base",
        descriptor=base_descriptor,
    )
    challenger = _candidate(
        candidate_id="models/challenger",
        descriptor=challenger_descriptor,
    )
    training = TrainingEvaluationBinding(
        job_id="training-job-1",
        base_candidate_id=champion.candidate_id,
        base_provider_id=champion.provider_id,
        base_model_id=champion.request_model,
        challenger_candidate_id=challenger.candidate_id,
        challenger_provider_id=challenger.provider_id,
        challenger_model_id=challenger.request_model,
        base_sha256=base_descriptor.sha256,
        challenger_sha256=challenger_descriptor.sha256,
        candidate_artifact_ref=challenger.candidate_id,
        frozen_package_sha256=_sha(b"frozen-package"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        base_descriptor_digest=base_descriptor.descriptor_digest,
        base_descriptor_registry_key=base_descriptor.registry_key,
        base_size_bytes=base_descriptor.size_bytes,
        descriptor_digest=challenger_descriptor.descriptor_digest,
        descriptor_registry_key=challenger_descriptor.registry_key,
        challenger_size_bytes=challenger_descriptor.size_bytes,
    )
    champion_binding = bind_champion_for_attested_evaluation(
        training_binding=training,
        champion=champion,
        descriptor=base_descriptor,
        champion_path=base_path,
        allowed_root=tmp_path,
    )
    return evaluation_set, training, champion_binding, champion, challenger


class _AttestedEffectPort:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_attested(
        self,
        request,
        *,
        binding,
    ) -> AttestedModelCompletionResult:
        self.calls += 1
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
        response = ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=binding.challenger_provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=binding.challenger_model_id,
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )
        return AttestedModelCompletionResult(
            response=response,
            attestation=attestation,
        )


async def _run_champion(
    *,
    binding: ChampionEvaluationBinding,
    champion: ModelCandidate,
    evaluation_set: EvaluationSet,
    port: _AttestedEffectPort,
    timeout_seconds: float = 5.0,
) -> AttestedChampionBenchmarkResult:
    return await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
        effect_port=port,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
        timeout_seconds=timeout_seconds,
        temperature=0.0,
    )


async def _run_challenger(
    *,
    binding: TrainingEvaluationBinding,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    port: _AttestedEffectPort,
    timeout_seconds: float = 5.0,
) -> AttestedChallengerBenchmarkResult:
    return await run_attested_challenger_benchmark(
        binding=binding,
        challenger=challenger,
        evaluation_set=evaluation_set,
        effect_port=port,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
        timeout_seconds=timeout_seconds,
        temperature=0.0,
    )


@pytest.mark.asyncio
async def test_champion_benchmark_reuses_attested_runner_and_emits_bound_evidence(
    tmp_path: Path,
) -> None:
    evaluation_set, training, binding, champion, _ = _authorities(tmp_path)
    port = _AttestedEffectPort()

    result = await _run_champion(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
        port=port,
    )

    assert port.calls == 2
    assert result.report.candidate == champion
    assert result.report.completion_rate == 1.0
    assert [receipt.case_id for receipt in result.case_receipts] == [
        "case-1",
        "case-2",
    ]
    payload = result.evidence_payload()
    assert payload["schema"] == "nika-attested-champion-benchmark-v1"
    assert payload["training_binding_sha256"] == training.binding_sha256
    assert payload["champion_binding_sha256"] == binding.binding_sha256
    assert payload["champion_candidate_id"] == champion.candidate_id
    assert payload["case_count"] == 2
    receipts = payload["case_receipts"]
    assert type(receipts) is list
    assert receipts[0]["schema"] == "nika-attested-champion-case-v1"
    assert receipts[0]["artifact_sha256"] == training.base_sha256
    assert len(receipts[0]["receipt_sha256"]) == 64
    assert result.revalidated().evidence_sha256 == result.evidence_sha256

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "question one" not in serialized
    assert "question two" not in serialized
    assert '"answer"' not in serialized


@pytest.mark.asyncio
async def test_old_new_pair_binds_same_training_and_execution_authority(
    tmp_path: Path,
) -> None:
    evaluation_set, training, binding, champion, challenger = _authorities(tmp_path)
    champion_result = await _run_champion(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
    )
    challenger_result = await _run_challenger(
        binding=training,
        challenger=challenger,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
    )

    result = pair_attested_old_new_evaluation(
        champion=champion_result,
        challenger=challenger_result,
    )

    payload = result.evidence_payload()
    assert payload["schema"] == "nika-attested-old-new-evaluation-v1"
    assert payload["training_binding_sha256"] == training.binding_sha256
    assert payload["evaluation_set_sha256"] == evaluation_set.content_sha256
    assert payload["champion_candidate_id"] == champion.candidate_id
    assert payload["challenger_candidate_id"] == challenger.candidate_id
    assert payload["case_count"] == 2
    assert payload["champion_evidence_sha256"] == champion_result.evidence_sha256
    assert payload["challenger_evidence_sha256"] == challenger_result.evidence_sha256
    assert result.revalidated().evidence_sha256 == result.evidence_sha256


@pytest.mark.asyncio
async def test_old_new_pair_rejects_different_training_base_authority(
    tmp_path: Path,
) -> None:
    evaluation_set, training, binding, champion, challenger = _authorities(tmp_path)
    champion_result = await _run_champion(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
    )
    changed_training = replace(
        training,
        base_model_id="substituted-base-route",
    )
    challenger_result = await _run_challenger(
        binding=changed_training,
        challenger=challenger,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
    )

    with pytest.raises(ValueError, match="training authority"):
        pair_attested_old_new_evaluation(
            champion=champion_result,
            challenger=challenger_result,
        )


@pytest.mark.asyncio
async def test_old_new_pair_rejects_different_execution_configuration(
    tmp_path: Path,
) -> None:
    evaluation_set, training, binding, champion, challenger = _authorities(tmp_path)
    champion_result = await _run_champion(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
        timeout_seconds=5.0,
    )
    challenger_result = await _run_challenger(
        binding=training,
        challenger=challenger,
        evaluation_set=evaluation_set,
        port=_AttestedEffectPort(),
        timeout_seconds=6.0,
    )

    with pytest.raises(ValueError, match="same evaluation authority"):
        pair_attested_old_new_evaluation(
            champion=champion_result,
            challenger=challenger_result,
        )


def test_direct_old_new_result_construction_is_disabled() -> None:
    with pytest.raises(TypeError):
        AttestedOldNewEvaluationResult(
            champion=object(),  # type: ignore[arg-type]
            challenger=object(),  # type: ignore[arg-type]
        )
