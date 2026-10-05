from __future__ import annotations

from collections.abc import Callable

from nika_core.diagnostics import (
    FoundryLocalModelHealthProbe,
    ModelHealthFact,
)
from nika_core.model_gateway.foundry_local import FoundryModelEvidence


class _Provider:
    def __init__(
        self,
        evidence: FoundryModelEvidence | None,
        calls: list[str],
        error: Exception | None = None,
    ) -> None:
        self._evidence = evidence
        self._calls = calls
        self._error = error

    def inspect_model(self, alias: str) -> FoundryModelEvidence:
        self._calls.append(alias)
        if self._error is not None:
            raise self._error
        assert self._evidence is not None
        return self._evidence


def _evidence(*, alias: str = "embedded-small", cached: bool) -> FoundryModelEvidence:
    return FoundryModelEvidence(
        model_id="embedded-small-generic-cpu",
        model_version="1",
        alias=alias,
        cached=cached,
        loaded=False,
        path="C:/model-cache/embedded-small" if cached else None,
        context_length=4096,
        input_modalities="text",
        output_modalities="text",
        capability_tags="chat",
        supports_tool_calling=False,
    )


def _factory(
    evidence: FoundryModelEvidence | None,
    calls: list[str],
    *,
    error: Exception | None = None,
    constructions: list[dict[str, object]] | None = None,
) -> Callable[..., _Provider]:
    def create(**kwargs: object) -> _Provider:
        if constructions is not None:
            constructions.append(dict(kwargs))
        return _Provider(evidence, calls, error)

    return create


def test_cached_foundry_model_is_ready_without_load_or_inference() -> None:
    calls: list[str] = []
    constructions: list[dict[str, object]] = []

    snapshot = FoundryLocalModelHealthProbe(
        model_id="embedded-small",
        provider_factory=_factory(
            _evidence(cached=True),
            calls,
            constructions=constructions,
        ),
    ).snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert calls == ["embedded-small"]
    assert constructions == [
        {"default_model": "embedded-small", "allow_download": False}
    ]


def test_uncached_foundry_model_is_present_but_not_ready() -> None:
    calls: list[str] = []

    snapshot = FoundryLocalModelHealthProbe(
        model_id="embedded-small",
        provider_factory=_factory(_evidence(cached=False), calls),
    ).snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.NO
    assert calls == ["embedded-small"]


def test_foundry_catalog_or_sdk_failure_is_unknown_not_absent() -> None:
    calls: list[str] = []

    snapshot = FoundryLocalModelHealthProbe(
        model_id="embedded-small",
        provider_factory=_factory(
            None,
            calls,
            error=RuntimeError("native catalog unavailable"),
        ),
    ).snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["embedded-small"]


def test_foundry_alias_mismatch_cannot_prove_readiness() -> None:
    calls: list[str] = []

    snapshot = FoundryLocalModelHealthProbe(
        model_id="embedded-small",
        provider_factory=_factory(
            _evidence(alias="different-model", cached=True),
            calls,
        ),
    ).snapshot()

    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["embedded-small"]


def test_invalid_foundry_identity_has_zero_provider_effects() -> None:
    constructions: list[dict[str, object]] = []

    snapshot = FoundryLocalModelHealthProbe(
        model_id="embedded\u200b-small",
        provider_factory=_factory(
            _evidence(cached=True),
            [],
            constructions=constructions,
        ),
    ).snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert constructions == []
