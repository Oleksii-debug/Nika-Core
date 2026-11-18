from __future__ import annotations

import pytest

from nika_core.intelligence.modes import IntelligenceMode
from nika_core.intelligence.provenance import (
    IntelligenceProvenance,
    IntelligenceResultStatus,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime


@pytest.mark.parametrize(
    ("provider_id", "provider_kind", "mode", "model"),
    (
        ("ollama", ProviderKind.LOCAL, IntelligenceMode.EXTERNAL_LOCAL, "qwen3:8b"),
        (
            "configured-api",
            ProviderKind.CLOUD,
            IntelligenceMode.EXTERNAL_API,
            "api-model",
        ),
    ),
)
def test_packaged_model_analysis_accepts_only_canonical_model_provenance(
    provider_id: str,
    provider_kind: ProviderKind,
    mode: IntelligenceMode,
    model: str,
) -> None:
    correlation = "task-1:v01:team-1:checker"
    provenance = IntelligenceProvenance(
        intelligence_mode=mode,
        provider_kind=provider_kind,
        provider_id=provider_id,
        model_fingerprint=model_identity_fingerprint(model),
        request_correlation_id=correlation,
        status=IntelligenceResultStatus.SUCCEEDED,
    ).to_payload()
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={
            "text": "bounded analysis",
            "provider_id": provider_id,
            "provider_kind": provider_kind.value,
            "model": model,
            "intelligence_provenance": provenance,
        },
    )

    assert V01PackagedThreeAgentRuntime._model_provenance(
        result,
        request_correlation_id=correlation,
    ) == provenance


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"request_correlation_id": "other:request"}, "correlation"),
        ({"provider_id": "other-provider"}, "provider"),
        ({"provider_kind": "cloud"}, "provider kind"),
        (
            {"model_fingerprint": model_identity_fingerprint("other-model")},
            "model identity",
        ),
        ({"status": "failed"}, "not successful"),
    ),
)
def test_packaged_model_analysis_rejects_tampered_provenance(
    mutation: dict[str, str],
    message: str,
) -> None:
    correlation = "task-1:v01:team-1:checker"
    provenance = IntelligenceProvenance(
        intelligence_mode=IntelligenceMode.EXTERNAL_LOCAL,
        provider_kind=ProviderKind.LOCAL,
        provider_id="ollama",
        model_fingerprint=model_identity_fingerprint("qwen3:8b"),
        request_correlation_id=correlation,
        status=IntelligenceResultStatus.SUCCEEDED,
    ).to_payload()
    provenance.update(mutation)
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={
            "text": "bounded analysis",
            "provider_id": "ollama",
            "provider_kind": "local",
            "model": "qwen3:8b",
            "intelligence_provenance": provenance,
        },
    )

    with pytest.raises(ValueError, match=message):
        V01PackagedThreeAgentRuntime._model_provenance(
            result,
            request_correlation_id=correlation,
        )


def test_packaged_model_analysis_rejects_missing_provenance() -> None:
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={
            "text": "bounded analysis",
            "provider_id": "ollama",
            "provider_kind": "local",
            "model": "qwen3:8b",
        },
    )

    with pytest.raises(TypeError, match="provenance is missing"):
        V01PackagedThreeAgentRuntime._model_provenance(
            result,
            request_correlation_id="task-1:v01:team-1:checker",
        )
