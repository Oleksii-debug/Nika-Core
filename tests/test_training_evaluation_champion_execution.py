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
    ModelFailureEffect,
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
    ChampionEvaluationBinding,
    bind_champion_for_attested_evaluation,
)
from nika_core.training_evaluation_champion_execution import (
    AttestedChampionBenchmarkResult,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_execution import TrainingEvaluationExecutionError


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_ATTESTOR_ID = "fixture-attestor"
_ATTESTOR_SHA256 = _sha(b"fixture-attestor-binary")
_PROVIDER_MANIFEST_SHA256 = _sha(b"champion-provider-manifest")


def _evaluation() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="held-out-champion",
        version="v1",
        provenance_ref="dataset:held-out-champion",
        license_ref="license:held-out",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-one",
                messages=(ModelMessage("user", "secret question one"),),
                expected_text="one",
            ),
            EvaluationCase(
                case_id="case-two",
                messages=(ModelMessage("user", "secret question two"),),
                expected_text="two",
            ),
        ),
    )


def _fixture(
    tmp_path: Path,
) -> tuple[
    ChampionEvaluationBinding,
    ModelCandidate,
    EvaluationSet,
]:
    evaluation = _evaluation()
    body = b"physical-champion-weights\n"
    path = tmp_path / "champion weights.bin"
    path.write_bytes(body)
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="champion-model",
        model_version="base-v1",
        source_reference="model:champion-base-v1",
        license_reference="license:champion-base-v1",
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
    training = TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id=champion.candidate_id,
        base_provider_id=descriptor.provider_id,
        base_model_id=descriptor.model_id,
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id="ollama",
        challenger_model_id="candidate-model",
        base_sha256=descriptor.sha256,
        challenger_sha256=_sha(b"challenger-weights"),
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"frozen-package"),
        execution_plan_sha256=_sha(b"training-execution-plan"),
        evaluation_set_sha256=evaluation.content_sha256,
        base_descriptor_digest=descriptor.descriptor_digest,
        base_descriptor_registry_key=descriptor.registry_key,
        base_size_bytes=descriptor.size_bytes,
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry-key"),
        challenger_size_bytes=len(b"challenger-weights"),
    )
    binding = bind_champion_for_attested_evaluation(
        training_binding=training,
        champion=champion,
        descriptor=descriptor,
        champion_path=path,
        allowed_root=tmp_path,
    )
    return binding, champion, evaluation


class _AttestedEffect:
    def __init__(
        self,
        *,
        wrong_artifact: bool = False,
        provider_manifest_sha256: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._wrong_artifact = wrong_artifact
        self._provider_manifest_sha256 = provider_manifest_sha256

    async def complete_attested(
        self,
        request,
        *,
        binding,
    ) -> AttestedModelCompletionResult:
        case_id = request.metadata["evaluation_case_id"]
        self.calls.append(case_id)
        answer = {
            "case-one": "one",
            "case-two": "two",
        }[case_id]
        artifact_sha256 = (
            _sha(b"wrong-artifact")
            if self._wrong_artifact
            else binding.challenger_sha256
        )
        return AttestedModelCompletionResult(
            response=ModelResponse(
                request_id=request.request_id,
                text=answer,
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model=binding.challenger_model_id,
                usage=ModelUsage(
                    input_tokens=2,
                    output_tokens=1,
                    total_tokens=3,
                ),
            ),
            attestation=LoadedModelArtifactAttestation(
                request_id=request.request_id,
                binding_sha256=binding.binding_sha256,
                provider_id=binding.challenger_provider_id,
                model_id=binding.challenger_model_id,
                artifact_sha256=artifact_sha256,
                descriptor_digest=binding.descriptor_digest,
                attestor_id=_ATTESTOR_ID,
                attestor_sha256=_ATTESTOR_SHA256,
                provider_manifest_sha256=self._provider_manifest_sha256,
            ),
        )


@pytest.mark.asyncio
async def test_complete_champion_benchmark_reuses_attested_runner(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    effect = _AttestedEffect()

    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=effect,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    assert effect.calls == ["case-one", "case-two"]
    assert result.report.candidate.candidate_id == champion.candidate_id
    assert result.report.completion_rate == 1.0
    assert result.report.task_pass_rate == 1.0
    assert [item.case_id for item in result.case_receipts] == [
        "case-one",
        "case-two",
    ]
    assert all(
        item.binding_sha256 == binding.binding_sha256
        for item in result.case_receipts
    )
    assert result.provider_manifest_sha256 is None
    assert "provider_manifest_sha256" not in result.evidence_payload()


@pytest.mark.asyncio
async def test_champion_evidence_preserves_provider_manifest(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_AttestedEffect(
            provider_manifest_sha256=_PROVIDER_MANIFEST_SHA256,
        ),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    assert result.provider_manifest_sha256 == _PROVIDER_MANIFEST_SHA256
    payload = result.evidence_payload()
    assert payload["provider_manifest_sha256"] == _PROVIDER_MANIFEST_SHA256
    assert all(
        item["provider_manifest_sha256"] == _PROVIDER_MANIFEST_SHA256
        for item in payload["case_receipts"]
    )


@pytest.mark.asyncio
async def test_champion_evidence_has_champion_schema_and_no_prompt_response(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_AttestedEffect(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    payload = result.evidence_payload()
    body = json.dumps(payload, ensure_ascii=False)

    assert payload["schema"] == "nika-attested-champion-benchmark-v1"
    assert payload["champion_candidate_id"] == champion.candidate_id
    assert payload["training_binding_sha256"] == binding.training_binding_sha256
    assert payload["champion_binding_sha256"] == binding.binding_sha256
    assert payload["case_count"] == 2
    assert "nika-attested-champion-case-v1" in body
    assert "secret question one" not in body
    assert "secret question two" not in body
    assert '"text"' not in body
    assert "challenger_candidate_id" not in body


@pytest.mark.asyncio
async def test_champion_loaded_artifact_mismatch_aborts_after_first_effect(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    effect = _AttestedEffect(wrong_artifact=True)

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await run_attested_champion_benchmark(
            binding=binding,
            champion=champion,
            evaluation_set=evaluation,
            effect_port=effect,
            expected_attestor_id=_ATTESTOR_ID,
            expected_attestor_sha256=_ATTESTOR_SHA256,
        )

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert effect.calls == ["case-one"]


@pytest.mark.asyncio
async def test_cross_evaluation_substitution_fails_before_effect(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    effect = _AttestedEffect()
    substituted = replace(
        evaluation,
        version="v2",
    )

    with pytest.raises(ValueError, match="evaluation set does not match"):
        await run_attested_champion_benchmark(
            binding=binding,
            champion=champion,
            evaluation_set=substituted,
            effect_port=effect,
            expected_attestor_id=_ATTESTOR_ID,
            expected_attestor_sha256=_ATTESTOR_SHA256,
        )

    assert effect.calls == []


@pytest.mark.asyncio
async def test_cross_candidate_substitution_fails_before_effect(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    effect = _AttestedEffect()
    substituted = replace(champion, candidate_id="models/not-base")

    with pytest.raises(ValueError, match="does not match training evaluation binding"):
        await run_attested_champion_benchmark(
            binding=binding,
            champion=substituted,
            evaluation_set=evaluation,
            effect_port=effect,
            expected_attestor_id=_ATTESTOR_ID,
            expected_attestor_sha256=_ATTESTOR_SHA256,
        )

    assert effect.calls == []


@pytest.mark.asyncio
async def test_champion_result_revalidation_rejects_receipt_artifact_substitution(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_AttestedEffect(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )
    forged = replace(
        result.case_receipts[0],
        artifact_sha256=_sha(b"forged-artifact"),
    )
    object.__setattr__(
        result,
        "case_receipts",
        (forged, result.case_receipts[1]),
    )

    with pytest.raises(ValueError, match="champion benchmark authority"):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_champion_result_revalidation_binds_receipts_to_run_identity(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_AttestedEffect(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )
    forged = replace(
        result.case_receipts[0],
        request_id="model-bench-" + ("a" * 32),
    )
    object.__setattr__(
        result,
        "case_receipts",
        (forged, result.case_receipts[1]),
    )

    with pytest.raises(ValueError, match="request identity"):
        result.evidence_payload()


def test_direct_attested_champion_result_construction_is_disabled() -> None:
    with pytest.raises(TypeError):
        AttestedChampionBenchmarkResult(
            binding=None,
            report=None,
            case_receipts=(),
            attestor_id=_ATTESTOR_ID,
            attestor_sha256=_ATTESTOR_SHA256,
        )


@pytest.mark.asyncio
async def test_champion_evidence_digest_is_stable_for_unchanged_result(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation = _fixture(tmp_path)
    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_AttestedEffect(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    assert result.evidence_sha256 == result.evidence_sha256
    assert len(result.evidence_sha256) == 64
