from __future__ import annotations

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def has_successful_inference(
        self,
        *,
        provider_id: str,
        model_id: str,
        route_identity: str,
    ) -> bool:
        self.calls += 1
        raise AssertionError("non-Ollama identity must not query inference evidence")


def _unexpected_client(**kwargs: object) -> None:
    raise AssertionError("non-Ollama identity must not construct an HTTP client")


@pytest.mark.parametrize("provider_id", ["foundry-local", "openai-compatible"])
def test_ollama_probe_rejects_noncanonical_provider_before_effects(provider_id: str) -> None:
    evidence = _Evidence()
    probe = OllamaModelHealthProbe(
        model_id="local-model:1",
        provider_id=provider_id,
        evidence_port=evidence,
        client_factory=_unexpected_client,
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == 0
