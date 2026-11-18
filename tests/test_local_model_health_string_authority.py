from __future__ import annotations

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _ExplosiveString(str):
    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile strip must not execute")

    def rstrip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile rstrip must not execute")


class _CatalogSpoof(str):
    def __eq__(self, other: object) -> bool:
        return other == str.__str__(self) or other == "local-model:1"

    def __hash__(self) -> int:
        return hash("local-model:1")


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
        raise AssertionError("invalid identity must not query inference evidence")


def _unexpected_client(**kwargs: object) -> None:
    raise AssertionError("invalid identity must not construct an HTTP client")


def _assert_rejected(
    *,
    model_id: str = "local-model:1",
    base_url: str = "http://localhost:11434",
    provider_id: str = "ollama",
) -> None:
    evidence = _Evidence()
    probe = OllamaModelHealthProbe(
        model_id=model_id,
        base_url=base_url,
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


def test_string_subclass_identities_fail_before_overloadable_operations_or_effects() -> None:
    _assert_rejected(model_id=_ExplosiveString("local-model:1"))
    _assert_rejected(provider_id=_ExplosiveString("ollama"))
    _assert_rejected(base_url=_ExplosiveString("http://localhost:11434"))


def test_model_id_subclass_cannot_spoof_catalog_membership() -> None:
    _assert_rejected(model_id=_CatalogSpoof("attacker-model"))
