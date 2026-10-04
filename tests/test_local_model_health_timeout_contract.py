from __future__ import annotations

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _ForgedNegativeInt(int):
    def __gt__(self, _other: object) -> bool:
        return True


class _ForgedNegativeFloat(float):
    def __gt__(self, _other: object) -> bool:
        return True


class _ExplodingEvidencePort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def has_successful_inference(
        self,
        *,
        provider_id: str,
        model_id: str,
        route_identity: str,
    ) -> bool | None:
        self.calls.append((provider_id, model_id, route_identity))
        raise AssertionError("inference evidence must not be queried for invalid timeout")


@pytest.mark.parametrize(
    ("timeout_seconds", "case_id"),
    [
        (float("nan"), "nan"),
        (float("inf"), "positive-infinity"),
        (float("-inf"), "negative-infinity"),
        (10**400, "unrepresentable-huge-int"),
        (_ForgedNegativeInt(-1), "hostile-negative-int-subclass"),
        (_ForgedNegativeFloat(-1.0), "hostile-negative-float-subclass"),
    ],
)
def test_invalid_timeout_fails_closed_before_health_transport(
    timeout_seconds: float,
    case_id: str,
) -> None:
    client_factory_calls: list[dict[str, object]] = []
    evidence = _ExplodingEvidencePort()

    def client_factory(**kwargs: object) -> object:
        client_factory_calls.append(kwargs)
        raise AssertionError(f"health transport must not start for {case_id}")

    probe = OllamaModelHealthProbe(
        model_id="local-model:1",
        timeout_seconds=timeout_seconds,
        evidence_port=evidence,
        client_factory=client_factory,
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == []
    assert client_factory_calls == []