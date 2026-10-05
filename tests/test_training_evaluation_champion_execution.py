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
from nika_core.training_evaluation_champion_execution import (
    AttestedChampionBenchmarkResult,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_execution import TrainingEvaluationExecutionError
from nika_core.training_evaluation_subprocess import RegistrySubprocessLoadedModelAttestor


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_ATTESTOR_ID = "fixture-attestor"
_ATTESTOR_SHA256 = _sha(b"fixture-attestor-binary")


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


def _base_descriptor(body: bytes) -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
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


def _fixture(
    tmp_path: Path,
) -> tuple[
    TrainingEvaluationBinding,
    ModelCandidate,
    EvaluationSet,
    Path,
    ModelArtifactDescriptor,
]:
    evaluation = _evaluation()
    body = b"physical-champion-weights\n"
    path = tmp_path / "champion weights.bin"
    path.write_bytes(body)
    descriptor = _base_descriptor(body)
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
    binding = TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id=champion.candidate_id,
        base_provider_id=champion.provider_id,
        base_model_id=champion.request_model,
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        base_sha256=descriptor.sha256,
        challenger_sha256=_sha(b"challenger-weights"),
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"frozen-package"),
        evaluation_set_sha256=evaluation.content_sha256,
        base_descriptor_digest=descriptor.descriptor_digest,
        base_descriptor_registry_key=descriptor.registry_key,
        base_size_bytes=descriptor.size_bytes,
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry-key"),
        challenger_size_bytes=len(b"challenger-weights"),
    )
    return binding, champion, evaluation, path, descriptor


class _AttestedEffect:
    def __init__(self, *, wrong_artifact: bool = False) -> None:
        self.calls: list[str] = []
        self._wrong_artifact = wrong_artifact

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
            ),
        )


def _success_script(tmp_path: Path) -> Path:
    path = tmp_path / "champion-evaluator.py"
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
    "text": "one",
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
    candidate_path: Path,
    descriptor: ModelArtifactDescriptor,
) -> RegistrySubprocessLoadedModelAttestor:
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


@pytest.mark.asyncio
async def test_complete_champion_benchmark_reuses_attested_runner(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    assert result.binding == binding
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


@pytest.mark.asyncio
async def test_champion_evidence_has_champion_schema_and_no_prompt_response(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    assert payload["binding_sha256"] == binding.binding_sha256
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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
    effect = _AttestedEffect()
    substituted = replace(evaluation, version="v2")

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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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
    binding, champion, evaluation, _, _ = _fixture(tmp_path)
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


@pytest.mark.asyncio
async def test_registry_subprocess_attests_bound_base_bytes_in_same_effect(
    tmp_path: Path,
) -> None:
    binding, champion, evaluation, path, descriptor = _fixture(tmp_path)
    one_case = replace(evaluation, cases=(evaluation.cases[0],))
    binding = replace(
        binding,
        evaluation_set_sha256=one_case.content_sha256,
    )
    attestor = _concrete_attestor(tmp_path, path, descriptor)

    result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=one_case,
        effect_port=attestor,
        expected_attestor_id=attestor.attestor_id,
        expected_attestor_sha256=attestor.attestor_sha256,
    )

    assert result.report.completion_rate == 1.0
    assert result.report.task_pass_rate == 1.0
    assert result.case_receipts[0].artifact_sha256 == binding.base_sha256
    assert (
        result.case_receipts[0].descriptor_digest
        == binding.base_descriptor_digest
    )
