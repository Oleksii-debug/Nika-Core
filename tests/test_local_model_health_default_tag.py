from __future__ import annotations

from collections.abc import Callable
from typing import Self

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(self, responses: dict[str, _Response], calls: list[str]) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self._calls.append(url)
        return self._responses[url]


def _factory(
    responses: dict[str, _Response], calls: list[str]
) -> Callable[..., _FakeClient]:
    def create(**kwargs: object) -> _FakeClient:
        assert kwargs == {
            "timeout": 2.0,
            "follow_redirects": False,
            "trust_env": False,
        }
        return _FakeClient(responses, calls)

    return create


def _probe(
    model_id: str,
    *,
    catalog_model: str,
    running_model: str | None,
) -> tuple[OllamaModelHealthProbe, list[str]]:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {
        f"{base}/api/tags": _Response({"models": [{"name": catalog_model}]})
    }
    if running_model is not None:
        responses[f"{base}/api/ps"] = _Response(
            {"models": [{"model": running_model}]}
        )
    return (
        OllamaModelHealthProbe(
            model_id=model_id,
            client_factory=_factory(responses, calls),
        ),
        calls,
    )


def test_untagged_ollama_model_matches_canonical_latest_catalog_and_runtime() -> None:
    probe, calls = _probe(
        "llama3.2",
        catalog_model="llama3.2:latest",
        running_model="llama3.2:latest",
    )

    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]


def test_explicit_ollama_tag_does_not_alias_to_latest() -> None:
    probe, calls = _probe(
        "llama3.2:q4_K_M",
        catalog_model="llama3.2:latest",
        running_model=None,
    )

    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.NO
    assert snapshot.model_ready is ModelHealthFact.NO
    assert calls == ["http://localhost:11434/api/tags"]


def test_registry_port_does_not_confuse_model_tag_detection() -> None:
    probe, _ = _probe(
        "registry.local:5000/team/model",
        catalog_model="registry.local:5000/team/model:latest",
        running_model="registry.local:5000/team/model:latest",
    )

    snapshot = probe.snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
