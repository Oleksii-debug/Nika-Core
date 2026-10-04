from __future__ import annotations

from typing import Self

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _Response:
    status_code = 200

    def __init__(self, body: object) -> None:
        self._body = body

    def json(self) -> object:
        return self._body


class _Client:
    def __init__(self, responses: dict[str, _Response], calls: list[str]) -> None:
        self.responses = responses
        self.calls = calls

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        return self.responses[url]


def _probe(
    *,
    catalog: dict[str, str],
    running: dict[str, str] | None = None,
) -> tuple[OllamaModelHealthProbe, list[str]]:
    base = "http://localhost:11434"
    responses = {f"{base}/api/tags": _Response({"models": [catalog]})}
    if running is not None:
        responses[f"{base}/api/ps"] = _Response({"models": [running]})
    calls: list[str] = []

    def client_factory(**kwargs: object) -> _Client:
        assert kwargs == {
            "timeout": 2.0,
            "follow_redirects": False,
            "trust_env": False,
        }
        return _Client(responses, calls)

    return (
        OllamaModelHealthProbe(
            model_id="selected:1",
            client_factory=client_factory,
        ),
        calls,
    )


@pytest.mark.parametrize(
    "catalog",
    [
        {"model": "other:1", "name": "selected:1"},
        {"model": "selected:1", "name": "other:1"},
    ],
)
def test_contradictory_catalog_identity_cannot_prove_presence(
    catalog: dict[str, str],
) -> None:
    probe, calls = _probe(catalog=catalog)
    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert calls == ["http://localhost:11434/api/tags"]


@pytest.mark.parametrize(
    "running",
    [
        {"model": "other:1", "name": "selected:1"},
        {"model": "selected:1", "name": "other:1"},
    ],
)
def test_contradictory_running_identity_cannot_prove_readiness(
    running: dict[str, str],
) -> None:
    probe, calls = _probe(
        catalog={"model": "selected:1", "name": "selected:1"},
        running=running,
    )
    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]


@pytest.mark.parametrize(
    "identity",
    [
        {"model": "selected:1", "name": "selected:1"},
        {"name": "selected:1"},
        {"model": "selected:1"},
    ],
)
def test_consistent_or_single_field_identity_preserves_valid_readiness(
    identity: dict[str, str],
) -> None:
    probe, calls = _probe(catalog=identity, running=identity)
    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert len(calls) == 2
