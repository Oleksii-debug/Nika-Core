from __future__ import annotations

from typing import Self

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _Evidence:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def has_successful_inference(
        self, *, provider_id: str, model_id: str, route_identity: str
    ) -> None:
        self.calls.append((provider_id, model_id, route_identity))


class _Response:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"models": [{"model": "selected:1", "name": "selected:1"}]}


class _Client:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        return _Response()


@pytest.mark.parametrize(
    "route",
    [
        "http://localhost:11434/?",
        "http://localhost:11434/#",
        "http://localhost:0",
        "http://localhost:11434 ",
        "http://local\nhost:11434",
        "http://local\thost:11434",
        "http://localhost:11434/\x7f",
    ],
)
def test_invalid_route_never_queries_evidence_or_starts_http(route: str) -> None:
    evidence = _Evidence()
    client_calls: list[dict[str, object]] = []

    def forbidden_client(**kwargs: object) -> None:
        client_calls.append(kwargs)
        raise AssertionError("invalid route must not construct an HTTP client")

    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        base_url=route,
        evidence_port=evidence,
        client_factory=forbidden_client,
    )
    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == []
    assert client_calls == []


@pytest.mark.parametrize(
    ("route", "canonical"),
    [
        ("http://localhost:11434/", "http://localhost:11434"),
        ("http://[::1]:11434/", "http://[::1]:11434"),
        ("https://127.0.0.1:11434", "https://127.0.0.1:11434"),
    ],
)
def test_valid_loopback_route_keeps_exact_endpoint_and_evidence_identity(
    route: str, canonical: str
) -> None:
    evidence = _Evidence()
    calls: list[str] = []

    def client_factory(**kwargs: object) -> _Client:
        assert kwargs == {
            "timeout": 2.0,
            "follow_redirects": False,
            "trust_env": False,
        }
        return _Client(calls)

    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        base_url=route,
        evidence_port=evidence,
        client_factory=client_factory,
    )
    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == [("ollama", "selected:1", canonical)]
    assert calls == [f"{canonical}/api/tags", f"{canonical}/api/ps"]
