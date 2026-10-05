from __future__ import annotations

import asyncio
import hashlib
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
    ModelBenchmarkRunner,
    ModelCandidate,
)
from nika_core.model_engineering.physical_candidate import (
    PhysicalCandidateEvaluationError,
    PhysicalCandidateGateway,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_artifacts import CandidateArtifactIntegrityError


def _artifact(tmp_path: Path) -> tuple[Path, Path, ModelArtifactDescriptor]:
    root = tmp_path / "models"
    root.mkdir()
    path = root / "candidate.bin"
    body = b"candidate-model-weights"
    path.write_bytes(body)
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="ollama-local",
        model_id="nika-candidate:1",
        model_version="candidate-1",
        source_reference="https://models.example.test/nika/candidate-1",
        license_reference="https://licenses.example.test/nika/candidate-1",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        capabilities=("text",),
    )
    return root, path, descriptor


def _candidate(
    descriptor: ModelArtifactDescriptor,
    *,
    provider_kind: ProviderKind = ProviderKind.LOCAL,
) -> ModelCandidate:
    return ModelCandidate(
        candidate_id="nika-candidate-1",
        provider_id=descriptor.provider_id,
        provider_kind=provider_kind,
        request_model=descriptor.model_id,
        expected_response_model=descriptor.model_id,
        engine_provenance_ref="pkg:ollama-adapter@1",
        engine_license_ref="https://licenses.example.test/ollama-adapter",
        model_provenance_ref=descriptor.source_reference,
        model_license_ref=descriptor.license_reference,
        model_sha256=descriptor.sha256,
    )


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="physical-candidate-smoke",
        version="1",
        provenance_ref="dataset:physical-candidate-smoke",
        license_ref="license:internal-evaluation",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="exact",
                messages=(ModelMessage("user", "test prompt"),),
                expected_text="expected",
            ),
        ),
    )


def _request(candidate: ModelCandidate) -> ModelRequest:
    return ModelRequest(
        request_id="physical-candidate-request",
        messages=(ModelMessage("user", "test prompt"),),
        model=candidate.request_model,
        provider_id=candidate.provider_id,
        provider_kind=candidate.provider_kind,
        privacy=PrivacyClass.PRIVATE,
    )


class _RecordingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text="expected",
            provider_id=request.provider_id or "",
            provider_kind=request.provider_kind or ProviderKind.NO_LLM,
            model=request.model or "",
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


def _gateway(
    tmp_path: Path,
) -> tuple[
    PhysicalCandidateGateway,
    _RecordingGateway,
    ModelCandidate,
    ModelArtifactDescriptor,
    Path,
]:
    root, path, descriptor = _artifact(tmp_path)
    candidate = _candidate(descriptor)
    delegate = _RecordingGateway()
    gateway = PhysicalCandidateGateway(
        delegate,
        candidate=candidate,
        descriptor=descriptor,
        artifact_path=path,
        allowed_root=root,
    )
    return gateway, delegate, candidate, descriptor, path


def test_model_benchmark_reverifies_physical_candidate_before_provider_effect(
    tmp_path: Path,
) -> None:
    gateway, delegate, candidate, _, _ = _gateway(tmp_path)
    runner = ModelBenchmarkRunner(gateway)

    report = asyncio.run(runner.benchmark(candidate, _evaluation_set()))

    assert delegate.calls == 1
    assert report.candidate_id == candidate.candidate_id
    assert report.completion_rate == 1.0
    assert report.task_pass_rate == 1.0
    assert report.weighted_quality_score == 1.0
    assert gateway.candidate_evidence_sha256 == candidate.evidence_sha256
    assert len(gateway.descriptor_digest) == 64


def test_same_size_artifact_tamper_fails_before_provider_effect(tmp_path: Path) -> None:
    gateway, delegate, candidate, _, path = _gateway(tmp_path)
    original = path.read_bytes()
    replacement = b"x" * len(original)
    assert replacement != original
    path.write_bytes(replacement)

    with pytest.raises(CandidateArtifactIntegrityError, match="digest does not match"):
        asyncio.run(gateway.complete(_request(candidate)))

    assert delegate.calls == 0


def test_descriptor_mutation_fails_before_provider_effect(tmp_path: Path) -> None:
    gateway, delegate, candidate, descriptor, _ = _gateway(tmp_path)
    object.__setattr__(descriptor, "sha256", "0" * 64)

    with pytest.raises(
        PhysicalCandidateEvaluationError,
        match="descriptor changed",
    ):
        asyncio.run(gateway.complete(_request(candidate)))

    assert delegate.calls == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider_id", "other-provider", "provider"),
        ("request_model", "other-model", "request model"),
        ("expected_response_model", "other-model", "response model"),
        (
            "model_provenance_ref",
            "https://models.example.test/other",
            "model provenance",
        ),
        (
            "model_license_ref",
            "https://licenses.example.test/other",
            "model license",
        ),
        ("model_sha256", "0" * 64, "model digest"),
    ],
)
def test_candidate_descriptor_substitution_is_rejected_at_binding(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    root, path, descriptor = _artifact(tmp_path)
    candidate = replace(_candidate(descriptor), **{field: value})

    with pytest.raises(PhysicalCandidateEvaluationError, match=message):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=candidate,
            descriptor=descriptor,
            artifact_path=path,
            allowed_root=root,
        )


def test_candidate_without_physical_digest_is_rejected(tmp_path: Path) -> None:
    root, path, descriptor = _artifact(tmp_path)
    candidate = replace(_candidate(descriptor), model_sha256=None)

    with pytest.raises(PhysicalCandidateEvaluationError, match="model_sha256"):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=candidate,
            descriptor=descriptor,
            artifact_path=path,
            allowed_root=root,
        )


def test_cloud_candidate_is_not_physical_local_evaluation_authority(
    tmp_path: Path,
) -> None:
    root, path, descriptor = _artifact(tmp_path)
    candidate = _candidate(descriptor, provider_kind=ProviderKind.CLOUD)

    with pytest.raises(PhysicalCandidateEvaluationError, match="local provider"):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=candidate,
            descriptor=descriptor,
            artifact_path=path,
            allowed_root=root,
        )


def test_cloud_descriptor_is_not_local_physical_evaluation_authority(
    tmp_path: Path,
) -> None:
    root, path, descriptor = _artifact(tmp_path)
    cloud = replace(descriptor, kind=ModelArtifactKind.CLOUD)

    with pytest.raises(PhysicalCandidateEvaluationError, match="local model artifact"):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=_candidate(cloud),
            descriptor=cloud,
            artifact_path=path,
            allowed_root=root,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider_id", "other-provider", "provider"),
        ("provider_kind", ProviderKind.CLOUD, "provider kind"),
        ("model", "other-model", "model"),
    ],
)
def test_request_identity_substitution_is_rejected_before_provider_effect(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    gateway, delegate, candidate, _, _ = _gateway(tmp_path)
    request = replace(_request(candidate), **{field: value})

    with pytest.raises(PhysicalCandidateEvaluationError, match=message):
        asyncio.run(gateway.complete(request))

    assert delegate.calls == 0


def test_fallback_provider_is_rejected_before_provider_effect(tmp_path: Path) -> None:
    gateway, delegate, candidate, _, _ = _gateway(tmp_path)
    request = replace(
        _request(candidate),
        fallback_provider_ids=("fallback-local",),
    )

    with pytest.raises(PhysicalCandidateEvaluationError, match="fallback"):
        asyncio.run(gateway.complete(request))

    assert delegate.calls == 0


def test_artifact_outside_allowed_root_is_rejected_at_binding(tmp_path: Path) -> None:
    _, path, descriptor = _artifact(tmp_path)
    allowed = tmp_path / "other-root"
    allowed.mkdir()

    with pytest.raises(CandidateArtifactIntegrityError, match="outside the allowed root"):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=_candidate(descriptor),
            descriptor=descriptor,
            artifact_path=path,
            allowed_root=allowed,
        )


def test_relative_artifact_path_is_rejected_before_file_access(tmp_path: Path) -> None:
    _, _, descriptor = _artifact(tmp_path)

    with pytest.raises(ValueError, match="artifact_path must be absolute"):
        PhysicalCandidateGateway(
            _RecordingGateway(),
            candidate=_candidate(descriptor),
            descriptor=descriptor,
            artifact_path=Path("candidate.bin"),
        )
