from __future__ import annotations

import asyncio

import pytest

from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkIdentityError,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_report_json,
)
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway

_DIGEST = "a" * 64


class _Provider:
    def __init__(self, digest: object = _DIGEST) -> None:
        self.digest = digest

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id="local",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "model",
            loaded_artifact_sha256=self.digest,  # type: ignore[arg-type]
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="prompt"),),
        model="model",
        provider_id="local",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
    )


def _candidate(*, digest: str | None = _DIGEST) -> ModelCandidate:
    return ModelCandidate(
        candidate_id="candidate",
        provider_id="local",
        provider_kind=ProviderKind.LOCAL,
        request_model="model",
        expected_response_model="model",
        engine_provenance_ref="engine",
        engine_license_ref="engine-license",
        model_provenance_ref="model",
        model_license_ref="model-license",
        model_sha256=digest,
    )


def _evaluation() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="held-out",
        version="1",
        provenance_ref="dataset",
        license_ref="license",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage(role="user", content="prompt"),),
                expected_text="answer",
            ),
        ),
    )


def test_gateway_snapshots_valid_loaded_artifact_attestation() -> None:
    gateway = ModelGateway()
    gateway.register(_Provider())

    response = asyncio.run(gateway.complete(_request()))

    assert type(response) is ModelResponse
    assert response.loaded_artifact_sha256 == _DIGEST


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, True, 7])
def test_gateway_rejects_noncanonical_loaded_artifact_attestation(digest: object) -> None:
    gateway = ModelGateway()
    gateway.register(_Provider(digest))

    with pytest.raises(ModelGatewayError, match="invalid success response"):
        asyncio.run(gateway.complete(_request()))


def test_digest_pinned_benchmark_requires_matching_loaded_artifact_attestation() -> None:
    gateway = ModelGateway()
    gateway.register(_Provider())
    report = asyncio.run(
        ModelBenchmarkRunner(
            gateway,
            clock=iter((1.0, 1.1)).__next__,
            run_id_factory=lambda: "attested-run",
        ).benchmark(_candidate(), _evaluation())
    )

    assert report.case_results[0].loaded_artifact_sha256 == _DIGEST
    assert f'"loaded_artifact_sha256":"{_DIGEST}"' in benchmark_report_json(report)


@pytest.mark.parametrize("digest", [None, "b" * 64])
def test_digest_pinned_benchmark_rejects_missing_or_substituted_attestation(
    digest: str | None,
) -> None:
    gateway = ModelGateway()
    gateway.register(_Provider(digest))

    with pytest.raises(ModelBenchmarkIdentityError):
        asyncio.run(
            ModelBenchmarkRunner(
                gateway,
                clock=iter((1.0, 1.1)).__next__,
                run_id_factory=lambda: "bad-attestation-run",
            ).benchmark(_candidate(), _evaluation())
        )


def test_unpinned_benchmark_rejects_unbound_loaded_artifact_attestation() -> None:
    gateway = ModelGateway()
    gateway.register(_Provider())

    with pytest.raises(
        ModelBenchmarkIdentityError,
        match="not bound to candidate evidence",
    ):
        asyncio.run(
            ModelBenchmarkRunner(
                gateway,
                clock=iter((1.0, 1.1)).__next__,
                run_id_factory=lambda: "unbound-attestation-run",
            ).benchmark(_candidate(digest=None), _evaluation())
        )
