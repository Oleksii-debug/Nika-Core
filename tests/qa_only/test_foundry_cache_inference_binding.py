from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.prove_foundry_local as proof
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelResponse,
    ModelUsage,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryModelEvidence


def _model_evidence(cache: Path) -> FoundryModelEvidence:
    return FoundryModelEvidence(
        model_id="model-id-v1",
        model_version="v1",
        alias="model-alias",
        cached=True,
        loaded=False,
        path=str(cache),
        context_length=None,
        input_modalities=None,
        output_modalities=None,
        capability_tags=None,
        supports_tool_calling=None,
    )


def _resource_snapshot() -> dict[str, object]:
    return {
        "system_cpu_percent": 1.0,
        "system_memory_percent": 1.0,
        "system_available_memory_bytes": 1024,
        "system_total_memory_bytes": 2048,
        "process_rss_bytes": 128,
        "process_cpu_seconds": 1.0,
    }


def test_physical_proof_fails_closed_if_cache_bytes_change_between_inferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    artifact = cache / "model.bin"
    artifact_a = b"qualified-artifact-A"
    artifact_b = b"substituted-artifact-B"
    artifact.write_bytes(artifact_a)
    bytes_seen_by_inference: list[bytes] = []

    class FakeProvider:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def inspect_model(self) -> FoundryModelEvidence:
            return _model_evidence(cache)

        def close(self) -> None:
            pass

    class FakeGateway:
        def register(self, _provider: object) -> None:
            pass

        async def complete(self, request: object) -> ModelResponse:
            bytes_seen_by_inference.append(artifact.read_bytes())
            if len(bytes_seen_by_inference) == 1:
                artifact.write_bytes(artifact_b)
            else:
                artifact.write_bytes(artifact_a)
            return ModelResponse(
                request_id=getattr(request, "request_id"),
                text="NIKA_FOUNDRY_LOCAL_OK",
                provider_id="foundry-local",
                provider_kind=ProviderKind.LOCAL,
                model="model-alias",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                latency_ms=1.0,
            )

    monkeypatch.setattr(proof.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        proof,
        "_installed_foundry_package",
        lambda: ("foundry-local-sdk-winml", "test-version"),
    )
    monkeypatch.setattr(proof, "_resource_snapshot", _resource_snapshot)
    monkeypatch.setattr(proof, "FoundryLocalProvider", FakeProvider)
    monkeypatch.setattr(proof, "ModelGateway", FakeGateway)

    args = SimpleNamespace(
        model="model-alias",
        model_id="model-id-v1",
        model_license="reviewed-license-ref",
        prompt="proof prompt",
        timeout=30.0,
        download_timeout=30.0,
        allow_download=False,
        hash_model_cache=True,
        max_cpu_percent=None,
        max_memory_percent=None,
        min_available_memory_gb=None,
    )

    try:
        result = asyncio.run(proof._run(args))
    except (ModelGatewayError, RuntimeError, ValueError):
        return

    pytest.fail(
        "physical proof returned success even though cache bytes changed A->B->A "
        f"across inference; seen={bytes_seen_by_inference!r}, "
        f"reported_digest={result.get('model_cache_digest')!r}"
    )
