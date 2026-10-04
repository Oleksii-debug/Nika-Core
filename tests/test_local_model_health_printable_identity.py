from __future__ import annotations

from typing import Self

import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _Response:
    status_code = 200

    def __init__(self, models: list[dict[str, object]]) -> None:
        self._models = models

    def json(self) -> dict[str, object]:
        return {"models": self._models}


class _Client:
    def __init__(self, responses: dict[str, _Response], calls: list[str]) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self._calls.append(url)
        return self._responses[url]


class _Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def has_successful_inference(
        self, *, provider_id: str, model_id: str, route_identity: str
    ) -> None:
        self.calls += 1
        raise AssertionError("unconfigured model must not query inference evidence")


@pytest.mark.parametrize(
    "model_id",
    [
        "selected\n:1",
        "selected\t:1",
        "selected\x7f:1",
        "selected\u202e:1",
        "selected\u200b:1",
    ],
)
def test_nonprintable_selected_model_fails_before_external_effects(model_id: str) -> None:
    evidence = _Evidence()
    calls: list[dict[str, object]] = []

    def forbidden_client(**kwargs: object) -> None:
        calls.append(kwargs)
        raise AssertionError("invalid model must not construct a client")

    probe = OllamaModelHealthProbe(
        model_id=model_id, evidence_port=evidence, client_factory=forbidden_client
    )
    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == 0
    assert calls == []


@pytest.mark.parametrize(
    "other_model",
    [
        " other:1",
        "other:1 ",
        "other\n:1",
        "other\x7f:1",
        "other\u202e:1",
        "other\u200b:1",
    ],
)
def test_malformed_catalog_entry_cannot_prove_presence_or_absence(
    other_model: str,
) -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {
        f"{base}/api/tags": _Response(
            [{"name": "selected:1"}, {"name": other_model}]
        ),
    }
    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=lambda **_kwargs: _Client(responses, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags"]


@pytest.mark.parametrize("bad_field", ["model", "name"])
def test_malformed_running_catalog_never_promotes_readiness(bad_field: str) -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {
        f"{base}/api/tags": _Response([{"model": "selected:1"}]),
        f"{base}/api/ps": _Response(
            [{"model": "selected:1"}, {bad_field: "other\u202e:1"}]
        ),
    }
    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=lambda **_kwargs: _Client(responses, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags", f"{base}/api/ps"]


class _RecursiveResponse(_Response):
    def __init__(self) -> None:
        super().__init__([])

    def json(self) -> dict[str, object]:
        raise RecursionError("deep untrusted Ollama JSON")


def test_recursive_tags_response_never_crashes_diagnostics() -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {f"{base}/api/tags": _RecursiveResponse()}
    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=lambda **_kwargs: _Client(responses, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags"]


def test_recursive_running_response_preserves_presence_without_claiming_readiness() -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {
        f"{base}/api/tags": _Response([{"model": "selected:1"}]),
        f"{base}/api/ps": _RecursiveResponse(),
    }
    probe = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=lambda **_kwargs: _Client(responses, calls),
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.YES
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags", f"{base}/api/ps"]
