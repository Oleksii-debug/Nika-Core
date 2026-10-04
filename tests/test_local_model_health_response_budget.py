from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe


LIMIT = 1024 * 1024


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.Client]:
    def factory(**kwargs: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _response(models: list[dict[str, object]]) -> httpx.Response:
    data = json.dumps({"models": models}).encode("utf-8")
    return httpx.Response(200, stream=httpx.ByteStream(data))


class _CountingBody(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_real_httpx_stream_keeps_positive_health_facts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["accept-encoding"] == "identity"
        return _response([{"model": "selected:1"}])

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.YES
    assert calls == ["/api/tags", "/api/ps"]


def test_declared_oversized_tags_rejected_before_body_read() -> None:
    body = _CountingBody([b"must never be read"])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, headers={"content-length": str(LIMIT + 1)}, stream=body
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]
    assert body.reads == 0
    assert body.closed


def test_chunked_tags_stop_at_byte_budget_without_followup() -> None:
    body = _CountingBody([b"x" * 16384 for _ in range(100)])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, stream=body)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]
    assert 0 < body.reads < 100
    assert body.closed


@pytest.mark.parametrize("content_encoding", ["gzip", "deflate", "br"])
def test_compressed_catalog_never_decompresses_untrusted_body(
    content_encoding: str,
) -> None:
    body = _CountingBody([b"not safe to decode"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": content_encoding}, stream=body
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert body.reads == 0
    assert body.closed


def test_oversized_running_catalog_preserves_verified_presence() -> None:
    calls: list[str] = []
    running = _CountingBody([b"ignored"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return _response([{"model": "selected:1"}])
        return httpx.Response(
            200, headers={"content-length": str(LIMIT + 1)}, stream=running
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags", "/api/ps"]
    assert running.reads == 0
    assert running.closed


@pytest.mark.parametrize("declared", ["abc", "-1", "0000000000000001"])
def test_malformed_content_length_is_unknown_not_absent(declared: str) -> None:
    body = _CountingBody([b"ignored"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": declared}, stream=body)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert body.reads == 0
    assert body.closed


def test_catalog_entry_budget_prevents_positive_or_negative_claims() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _response([{"model": "selected:1"}] * 4097)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]


@pytest.mark.parametrize("model_id", ["x" * 513, "é" * 300])
def test_oversized_selected_identity_has_zero_external_effects(model_id: str) -> None:
    calls = 0

    def forbidden_factory(**_kwargs: object) -> httpx.Client:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid model must not create HTTP clients")

    snapshot = OllamaModelHealthProbe(
        model_id=model_id, client_factory=forbidden_factory
    ).snapshot()
    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert calls == 0


@pytest.mark.parametrize("identity", ["x" * 513, "é" * 300])
def test_oversized_catalog_identity_cannot_prove_presence(identity: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _response([{"model": "selected:1"}, {"model": identity}])

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]

def test_preconsumed_response_fails_closed_without_crashing_probe() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # httpx.Response(json=...) is already consumed before Client.stream().
        return httpx.Response(200, json={"models": [{"model": "selected:1"}]})

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]


class _ReadTimeoutBody(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"models":'
        raise httpx.ReadTimeout("body stalled after HTTP response headers")

    def close(self) -> None:
        self.closed = True


def test_tags_body_read_timeout_preserves_http_reachability() -> None:
    calls: list[str] = []
    broken = _ReadTimeoutBody()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, stream=broken)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]
    assert broken.closed


def test_running_body_read_timeout_preserves_catalog_presence() -> None:
    calls: list[str] = []
    broken = _ReadTimeoutBody()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return _response([{"model": "selected:1"}])
        return httpx.Response(200, stream=broken)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1", client_factory=_client_factory(handler)
    ).snapshot()
    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags", "/api/ps"]
    assert broken.closed
