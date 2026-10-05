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
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
            200,
            headers={"content-length": str(LIMIT + 1)},
            stream=body,
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
            200,
            headers={"content-encoding": content_encoding},
            stream=body,
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
            200,
            headers={"content-length": str(LIMIT + 1)},
            stream=running,
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
        return httpx.Response(
            200,
            headers={"content-length": declared},
            stream=body,
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
        model_id="selected:1",
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]


def test_preconsumed_response_fails_closed_without_crashing_probe() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"models": [{"model": "selected:1"}]},
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]

class _ControlledDeadlineTimer:
    def __init__(
        self,
        interval: float,
        function: Callable[..., object],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> None:
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def join(self) -> None:
        self.joined = True

    def fire(self) -> None:
        assert self.started
        assert not self.cancelled
        self.function(*self.args, **self.kwargs)


class _ImmediateDeadlineTimer(_ControlledDeadlineTimer):
    def start(self) -> None:
        super().start()
        self.fire()


def test_deadline_before_first_request_does_not_prove_reachability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers: list[_ImmediateDeadlineTimer] = []

    def timer_factory(
        interval: float,
        function: Callable[..., object],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> _ImmediateDeadlineTimer:
        timer = _ImmediateDeadlineTimer(interval, function, args, kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr(
        "nika_core.diagnostics.model_health.Timer",
        timer_factory,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _response([{"model": "selected:1"}])

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        timeout_seconds=30.0,
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == []
    assert len(timers) == 1
    assert timers[0].started
    assert timers[0].cancelled
    assert timers[0].joined


class _DeadlineProgressBody(httpx.SyncByteStream):
    def __init__(self, timers: list[_ControlledDeadlineTimer]) -> None:
        self.timers = timers
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield b'{"models":['
        assert self.timers
        self.timers[0].fire()
        yield b'{"model":"selected:1"}]}'

    def close(self) -> None:
        self.closed = True


def test_total_health_deadline_stops_progress_and_skips_second_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers: list[_ControlledDeadlineTimer] = []

    def timer_factory(
        interval: float,
        function: Callable[..., object],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> _ControlledDeadlineTimer:
        timer = _ControlledDeadlineTimer(interval, function, args, kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr(
        "nika_core.diagnostics.model_health.Timer",
        timer_factory,
    )
    body = _DeadlineProgressBody(timers)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, stream=body)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        timeout_seconds=30.0,
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]
    assert body.closed
    assert len(timers) == 1
    assert timers[0].interval == 30.0
    assert timers[0].cancelled
    assert timers[0].joined


def test_total_health_deadline_on_running_catalog_preserves_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers: list[_ControlledDeadlineTimer] = []

    def timer_factory(
        interval: float,
        function: Callable[..., object],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> _ControlledDeadlineTimer:
        timer = _ControlledDeadlineTimer(interval, function, args, kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr(
        "nika_core.diagnostics.model_health.Timer",
        timer_factory,
    )
    running = _DeadlineProgressBody(timers)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return _response([{"model": "selected:1"}])
        return httpx.Response(200, stream=running)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        timeout_seconds=30.0,
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags", "/api/ps"]
    assert running.closed
    assert len(timers) == 1
    assert timers[0].cancelled
    assert timers[0].joined


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
        model_id="selected:1",
        client_factory=_client_factory(handler),
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
        model_id="selected:1",
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags", "/api/ps"]
    assert broken.closed

def _raw_response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, stream=httpx.ByteStream(payload))


@pytest.mark.parametrize(
    "payload",
    [
        (
            b'{"models":[{"model":"other:1"}],'
            b'"models":[{"model":"selected:1"}]}'
        ),
        b'{"models":[{"model":"other:1","model":"selected:1"}]}',
        b'{"models":[{"name":"other:1","name":"selected:1"}]}',
        b'{"models":[{"model":"selected:1","score":NaN}]}',
    ],
)
def test_ambiguous_tags_json_is_unknown(payload: bytes) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _raw_response(payload)

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags"]


def test_duplicate_running_identity_cannot_prove_readiness() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return _response([{"model": "selected:1"}])
        return _raw_response(
            b'{"models":[{"model":"other:1","model":"selected:1"}]}'
        )

    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=_client_factory(handler),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["/api/tags", "/api/ps"]

class _UnreadResponseClient:
    def __enter__(self) -> "_UnreadResponseClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, _url: str) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'{"models":[{"model":"selected:1"}]}'),
        )


def test_unread_exact_response_is_unknown_not_exception() -> None:
    snapshot = OllamaModelHealthProbe(
        model_id="selected:1",
        client_factory=lambda **_kwargs: _UnreadResponseClient(),
    ).snapshot()

    assert snapshot.reachable is ModelHealthFact.YES
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN

