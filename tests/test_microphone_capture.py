from __future__ import annotations

import asyncio

import pytest

from nika_core.microphone_capture import (
    MicrophoneCaptureAdapterError,
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureFailureCode,
    MicrophoneCapturePolicy,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
    MicrophoneCaptureService,
    MicrophoneCaptureStatus,
    UnavailableMicrophoneCaptureAdapter,
)


def _request(**overrides: object) -> MicrophoneCaptureRequest:
    values: dict[str, object] = {
        "request_id": "capture-1",
        "provider_id": "local-microphone",
        "device_id": "default-input",
        "sample_rate_hz": 16_000,
        "sample_count": 1_600,
    }
    values.update(overrides)
    return MicrophoneCaptureRequest(**values)  # type: ignore[arg-type]


class _FakeAdapter:
    def __init__(
        self,
        *,
        capabilities: MicrophoneCaptureCapabilities | None = None,
        response: MicrophoneCaptureResponse | None = None,
    ) -> None:
        self._capabilities = capabilities or MicrophoneCaptureCapabilities(
            provider_id="local-microphone",
            device_id="default-input",
        )
        self.response = response
        self.calls = 0

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.calls += 1
        if self.response is not None:
            return self.response
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=b"\x01\x00" * request.sample_count,
            latency_ms=12.5,
        )


def test_success_returns_transient_pcm_and_minimized_evidence() -> None:
    adapter = _FakeAdapter()
    result = asyncio.run(MicrophoneCaptureService(adapter).capture(_request()))

    assert result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
    assert result.pcm_s16le == b"\x01\x00" * 1_600
    assert result.evidence.audio_byte_count == 3_200
    assert result.evidence.audio_sha256 is not None
    payload = result.evidence.as_dict()
    assert payload["provider_id"] == "local-microphone"
    assert payload["device_id_sha256"] != "default-input"
    assert "default-input" not in repr(payload)
    assert "pcm_s16le" not in payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", ""),
        ("provider_id", "has space"),
        ("device_id", "x" * 129),
        ("sample_rate_hz", True),
        ("sample_rate_hz", 7_999),
        ("sample_count", 0),
        ("sample_count", 16_000 * 31),
    ],
)
def test_request_rejects_noncanonical_or_unbounded_values(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(**{field: value})


def test_policy_rejects_bool_nonfinite_and_huge_timeout() -> None:
    for value in (True, float("nan"), float("inf"), 10**1000):
        with pytest.raises((TypeError, ValueError)):
            MicrophoneCapturePolicy(timeout_seconds=value)  # type: ignore[arg-type]


def test_resource_limit_fails_before_adapter_effect() -> None:
    adapter = _FakeAdapter()
    request = _request(policy=MicrophoneCapturePolicy(max_audio_bytes=2_000))

    result = asyncio.run(MicrophoneCaptureService(adapter).capture(request))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.RESOURCE_LIMIT
    assert adapter.calls == 0


def test_route_mismatch_fails_before_adapter_effect() -> None:
    adapter = _FakeAdapter(
        capabilities=MicrophoneCaptureCapabilities(
            provider_id="different-provider",
            device_id="default-input",
        )
    )

    result = asyncio.run(MicrophoneCaptureService(adapter).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ROUTE_MISMATCH
    assert adapter.calls == 0


def test_unsupported_sample_rate_fails_before_adapter_effect() -> None:
    adapter = _FakeAdapter(
        capabilities=MicrophoneCaptureCapabilities(
            provider_id="local-microphone",
            device_id="default-input",
            min_sample_rate_hz=44_100,
            max_sample_rate_hz=48_000,
        )
    )

    result = asyncio.run(MicrophoneCaptureService(adapter).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.RESOURCE_LIMIT
    assert adapter.calls == 0


@pytest.mark.parametrize("field", ["request_id", "provider_id", "device_id", "sample_rate_hz"])
def test_response_identity_substitution_fails_closed(field: str) -> None:
    values: dict[str, object] = {
        "request_id": "capture-1",
        "provider_id": "local-microphone",
        "device_id": "default-input",
        "sample_rate_hz": 16_000,
    }
    values[field] = "other" if field != "sample_rate_hz" else 48_000
    response = MicrophoneCaptureResponse(
        **values,  # type: ignore[arg-type]
        pcm_s16le=b"\x00\x00" * 1_600,
    )
    result = asyncio.run(
        MicrophoneCaptureService(_FakeAdapter(response=response)).capture(_request())
    )

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ROUTE_MISMATCH
    assert result.pcm_s16le is None


def test_wrong_pcm_length_is_rejected() -> None:
    response = MicrophoneCaptureResponse(
        request_id="capture-1",
        provider_id="local-microphone",
        device_id="default-input",
        sample_rate_hz=16_000,
        pcm_s16le=b"\x00\x00",
    )

    result = asyncio.run(
        MicrophoneCaptureService(_FakeAdapter(response=response)).capture(_request())
    )

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.INVALID_RESPONSE


def test_wrong_pcm_type_is_rejected() -> None:
    response = MicrophoneCaptureResponse(
        request_id="capture-1",
        provider_id="local-microphone",
        device_id="default-input",
        sample_rate_hz=16_000,
        pcm_s16le=bytearray(b"\x00\x00" * 1_600),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        MicrophoneCaptureService(_FakeAdapter(response=response)).capture(_request())
    )

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.INVALID_RESPONSE


def test_noncanonical_response_identity_object_cannot_spoof_equality() -> None:
    class _EqualToEverything:
        def __eq__(self, other: object) -> bool:
            return True

    response = MicrophoneCaptureResponse(
        request_id=_EqualToEverything(),  # type: ignore[arg-type]
        provider_id="local-microphone",
        device_id="default-input",
        sample_rate_hz=16_000,
        pcm_s16le=b"\x00\x00" * 1_600,
    )

    result = asyncio.run(
        MicrophoneCaptureService(_FakeAdapter(response=response)).capture(_request())
    )

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ROUTE_MISMATCH


def test_capability_drift_after_effect_fails_closed() -> None:
    first = MicrophoneCaptureCapabilities(
        provider_id="local-microphone",
        device_id="default-input",
    )
    second = MicrophoneCaptureCapabilities(
        provider_id="local-microphone",
        device_id="replacement-input",
    )

    class _DriftingAdapter(_FakeAdapter):
        def __init__(self) -> None:
            super().__init__(capabilities=first)
            self.reads = 0

        @property
        def capabilities(self) -> MicrophoneCaptureCapabilities:
            self.reads += 1
            return first if self.reads == 1 else second

    result = asyncio.run(MicrophoneCaptureService(_DriftingAdapter()).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ROUTE_MISMATCH
    assert result.pcm_s16le is None


def test_noncanonical_capabilities_are_rejected_without_effect() -> None:
    class _CapabilitiesSubclass(MicrophoneCaptureCapabilities):
        pass

    adapter = _FakeAdapter(
        capabilities=_CapabilitiesSubclass(
            provider_id="local-microphone",
            device_id="default-input",
        )
    )

    result = asyncio.run(MicrophoneCaptureService(adapter).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ADAPTER_ERROR
    assert adapter.calls == 0


def test_capabilities_exception_is_minimized_to_adapter_error() -> None:
    class _BrokenCapabilities(_FakeAdapter):
        @property
        def capabilities(self) -> MicrophoneCaptureCapabilities:
            raise RuntimeError("SECRET-CANARY")

    result = asyncio.run(MicrophoneCaptureService(_BrokenCapabilities()).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ADAPTER_ERROR
    assert "SECRET-CANARY" not in repr(result.evidence.as_dict())


def test_unknown_adapter_exception_is_minimized() -> None:
    class _BrokenAdapter(_FakeAdapter):
        async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
            del request
            raise RuntimeError("SECRET-CANARY")

    result = asyncio.run(MicrophoneCaptureService(_BrokenAdapter()).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ADAPTER_ERROR
    assert "SECRET-CANARY" not in repr(result.evidence.as_dict())


def test_typed_unavailable_adapter_returns_unavailable_without_audio() -> None:
    request = _request(
        provider_id="microphone-unavailable",
        device_id="unavailable",
    )

    result = asyncio.run(
        MicrophoneCaptureService(UnavailableMicrophoneCaptureAdapter()).capture(request)
    )

    assert result.evidence.status is MicrophoneCaptureStatus.UNAVAILABLE
    assert result.evidence.error_code is MicrophoneCaptureFailureCode.UNAVAILABLE
    assert result.pcm_s16le is None


def test_timeout_returns_typed_failure() -> None:
    class _NeverReturns(_FakeAdapter):
        async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
            del request
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    request = _request(policy=MicrophoneCapturePolicy(timeout_seconds=0.001))
    result = asyncio.run(MicrophoneCaptureService(_NeverReturns()).capture(request))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.TIMEOUT
    assert result.evidence.retryable is True


def test_caller_cancellation_propagates() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class _NeverReturns(_FakeAdapter):
            async def capture(
                self,
                request: MicrophoneCaptureRequest,
            ) -> MicrophoneCaptureResponse:
                del request
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        task = asyncio.create_task(MicrophoneCaptureService(_NeverReturns()).capture(_request()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_adapter_error_message_is_not_reported() -> None:
    class _TypedFailure(_FakeAdapter):
        async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
            del request
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "SECRET-CANARY",
                retryable=True,
            )

    result = asyncio.run(MicrophoneCaptureService(_TypedFailure()).capture(_request()))

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.ADAPTER_ERROR
    assert result.evidence.retryable is True
    assert "SECRET-CANARY" not in repr(result.evidence.as_dict())


def test_invalid_latency_is_rejected() -> None:
    response = MicrophoneCaptureResponse(
        request_id="capture-1",
        provider_id="local-microphone",
        device_id="default-input",
        sample_rate_hz=16_000,
        pcm_s16le=b"\x00\x00" * 1_600,
        latency_ms=float("inf"),
    )

    result = asyncio.run(
        MicrophoneCaptureService(_FakeAdapter(response=response)).capture(_request())
    )

    assert result.evidence.error_code is MicrophoneCaptureFailureCode.INVALID_RESPONSE
