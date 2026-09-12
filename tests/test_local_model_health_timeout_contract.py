from __future__ import annotations

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


@pytest.mark.parametrize(
    ("timeout_seconds", "case_id"),
    [
        (float("nan"), "nan"),
        (float("inf"), "positive-infinity"),
        (float("-inf"), "negative-infinity"),
        (10**400, "unrepresentable-huge-int"),
    ],
)
def test_invalid_timeout_fails_closed_before_health_transport(
    timeout_seconds: float,
    case_id: str,
) -> None:
    client_factory_calls: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> object:
        client_factory_calls.append(kwargs)
        raise AssertionError(f"health transport must not start for {case_id}")

    probe = OllamaModelHealthProbe(
        model_id="local-model:1",
        timeout_seconds=timeout_seconds,
        client_factory=client_factory,
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert client_factory_calls == []
