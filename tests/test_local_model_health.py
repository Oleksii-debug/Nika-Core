from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from nika_core.diagnostics import (
    HealthStatus,
    ModelHealthFact,
    ModelHealthSnapshot,
    OllamaModelHealthProbe,
)


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, responses: dict[str, object], calls: list[str]) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self._calls.append(url)
        value = self._responses[url]
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, _Response)
        return value


def _factory(
    responses: dict[str, object],
    calls: list[str],
) -> Callable[..., _FakeClient]:
    def create(**kwargs: object) -> _FakeClient:
        assert kwargs["timeout"] == 2.0
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return _FakeClient(responses, calls)

    return create


class _Evidence:
    def __init__(self, result: bool | None) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def has_successful_inference(self, *, provider_id: str, model_id: str) -> bool | None:
        self.calls.append((provider_id, model_id))
        return self.result


def _probe(
    *,
    tags: object,
    running: object | None = None,
    evidence: _Evidence | None = None,
) -> tuple[OllamaModelHealthProbe, list[str]]:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses: dict[str, object] = {f"{base}/api/tags": tags}
    if running is not None:
        responses[f"{base}/api/ps"] = running
    return (
        OllamaModelHealthProbe(
            model_id="local-model:1",
            base_url=base,
            evidence_port=evidence,
            client_factory=_factory(responses, calls),
        ),
        calls,
    )


def test_server_reachable_but_model_absent_is_not_ready() -> None:
    probe, calls = _probe(
        tags=_Response({"models": [{"name": "other-model:1"}]}),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.NO
    assert snapshot.model_ready is ModelHealthFact.NO
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert calls == ["http://localhost:11434/api/tags"]
    assert snapshot.to_health_check().status is HealthStatus.FAIL


def test_present_model_without_runtime_readiness_stays_unknown() -> None:
    probe, calls = _probe(
        tags=_Response({"models": [{"name": "local-model:1"}]}),
        running=_Response({"models": []}),
    )

    snapshot = probe.snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]
    assert snapshot.to_health_check().status is HealthStatus.WARN


def test_running_model_is_ready_but_not_inference_proven() -> None:
    probe, _ = _probe(
        tags=_Response({"models": [{"model": "local-model:1"}]}),
        running=_Response({"models": [{"name": "local-model:1"}]}),
    )

    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert snapshot.to_health_check().status is HealthStatus.WARN


def test_exact_prior_inference_evidence_is_separate_from_readiness() -> None:
    evidence = _Evidence(True)
    probe, calls = _probe(
        tags=_Response({"models": [{"model": "local-model:1"}]}),
        running=_Response({"models": [{"model": "local-model:1"}]}),
        evidence=evidence,
    )

    snapshot = probe.snapshot()

    assert snapshot.as_dict() == {
        "configured": "yes",
        "reachable": "yes",
        "model_present": "yes",
        "model_ready": "yes",
        "inference_proven": "yes",
    }
    assert snapshot.to_health_check().status is HealthStatus.PASS
    assert evidence.calls == [("ollama", "local-model:1")]
    assert calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]


def test_malformed_catalog_proves_reachability_only() -> None:
    probe, calls = _probe(tags=_Response({"models": "not-a-list"}))

    snapshot = probe.snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["http://localhost:11434/api/tags"]
    assert snapshot.to_health_check().status is HealthStatus.WARN


def test_transport_failure_does_not_claim_presence_or_readiness() -> None:
    request = httpx.Request("GET", "http://localhost:11434/api/tags")
    failure = httpx.ConnectError("offline", request=request)
    probe, calls = _probe(tags=failure)

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.NO
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["http://localhost:11434/api/tags"]
    assert snapshot.to_health_check().status is HealthStatus.FAIL


def test_invalid_configuration_performs_no_network_calls() -> None:
    calls: list[str] = []
    probe = OllamaModelHealthProbe(
        model_id=" ",
        client_factory=_factory({}, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com:11434",
        "http://user:secret@localhost:11434",
        "http://localhost.:11434",
    ],
)
def test_non_loopback_or_ambiguous_configuration_performs_no_network_calls(
    base_url: str,
) -> None:
    calls: list[str] = []
    probe = OllamaModelHealthProbe(
        model_id="local-model:1",
        base_url=base_url,
        client_factory=_factory({}, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert calls == []


def test_readiness_endpoint_failure_does_not_erase_presence() -> None:
    base = "http://localhost:11434"
    request = httpx.Request("GET", f"{base}/api/ps")
    probe, calls = _probe(
        tags=_Response({"models": [{"name": "local-model:1"}]}),
        running=httpx.ConnectError("ps unavailable", request=request),
    )

    snapshot = probe.snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags", f"{base}/api/ps"]


def test_snapshot_rejects_ready_from_reachability_alone() -> None:
    with pytest.raises(ValueError, match="model_ready=yes requires"):
        ModelHealthSnapshot(
            configured=ModelHealthFact.YES,
            reachable=ModelHealthFact.YES,
            model_present=ModelHealthFact.NO,
            model_ready=ModelHealthFact.YES,
            inference_proven=ModelHealthFact.UNKNOWN,
        )
