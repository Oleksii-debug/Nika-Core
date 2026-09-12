from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind
from nika_core.speech_to_text import (
    SpeechAudio,
    SpeechAudioFormat,
    SpeechToTextAdapterError,
    SpeechToTextAdapterResponse,
    SpeechToTextFailureCode,
    SpeechToTextPolicy,
    SpeechToTextRequest,
    SpeechToTextService,
    SpeechToTextStatus,
    UnavailableSpeechToTextAdapter,
)


class _RecordingAdapter:
    provider_kind = ProviderKind.LOCAL

    def __init__(self, response: SpeechToTextAdapterResponse) -> None:
        self.response = response
        self.calls: list[SpeechToTextRequest] = []

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        self.calls.append(request)
        return self.response


class _CloudRecordingAdapter(_RecordingAdapter):
    provider_kind = ProviderKind.CLOUD


class _UntypedRecordingAdapter:
    def __init__(self, response: SpeechToTextAdapterResponse) -> None:
        self.response = response
        self.calls: list[SpeechToTextRequest] = []

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        self.calls.append(request)
        return self.response


class _ExplodingAdapter:
    provider_kind = ProviderKind.LOCAL

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        del request
        raise RuntimeError("synthetic provider diagnostic must not escape")


class _TypedFailureAdapter:
    provider_kind = ProviderKind.LOCAL

    def __init__(self, code: SpeechToTextFailureCode, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        del request
        raise SpeechToTextAdapterError(
            self.code,
            "synthetic adapter diagnostic",
            retryable=self.retryable,
        )


class _BlockedAdapter:
    provider_kind = ProviderKind.LOCAL

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        self.entered.set()
        await self.release.wait()
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="done",
        )


def _audio(data: bytes = b"RIFFsynthetic-audio") -> SpeechAudio:
    return SpeechAudio(
        data=data,
        audio_format=SpeechAudioFormat.WAV,
        sample_rate_hz=16_000,
        channels=1,
    )


def _request(
    *,
    audio: SpeechAudio | None = None,
    policy: SpeechToTextPolicy | None = None,
) -> SpeechToTextRequest:
    return SpeechToTextRequest(
        request_id="stt-1",
        provider_id="local-stt",
        model="uk-small-v1",
        audio=audio or _audio(),
        language="uk",
        privacy=PrivacyClass.SENSITIVE,
        policy=policy or SpeechToTextPolicy(),
    )


def test_local_success_returns_text_but_evidence_contains_no_audio_or_transcript() -> None:
    request = _request()
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="Привіт, Ніко.",
            detected_language="uk",
            latency_ms=42.5,
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text == "Привіт, Ніко."
    assert result.evidence.status is SpeechToTextStatus.SUCCEEDED
    assert result.evidence.detected_language == "uk"
    assert result.evidence.transcript_chars == len("Привіт, Ніко.")
    assert result.evidence.transcript_sha256 is not None
    assert result.evidence.audio_sha256
    assert adapter.calls == [request]

    durable = result.evidence.as_dict()
    rendered = repr(durable)
    assert "Привіт" not in rendered
    assert "RIFFsynthetic-audio" not in rendered
    assert "data" not in durable
    assert "text" not in durable


def test_nonlocal_adapter_is_rejected_before_audio_is_sent() -> None:
    request = _request()
    adapter = _CloudRecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="must not run",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.status is SpeechToTextStatus.FAILED
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert adapter.calls == []


def test_untyped_adapter_is_rejected_before_audio_is_sent() -> None:
    request = _request()
    adapter = _UntypedRecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="must not run",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))  # type: ignore[arg-type]

    assert result.text is None
    assert result.evidence.status is SpeechToTextStatus.FAILED
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert adapter.calls == []


def test_audio_bound_fails_before_adapter_call_and_full_digest() -> None:
    request = _request(
        audio=_audio(b"12345"),
        policy=SpeechToTextPolicy(max_audio_bytes=4),
    )
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id="stt-1",
            provider_id="local-stt",
            model="uk-small-v1",
            text="must not run",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.status is SpeechToTextStatus.FAILED
    assert result.evidence.error_code is SpeechToTextFailureCode.RESOURCE_LIMIT
    assert result.evidence.audio_sha256 is None
    assert adapter.calls == []


def test_transcript_bound_fails_closed() -> None:
    request = _request(policy=SpeechToTextPolicy(max_transcript_chars=3))
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="four",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.RESOURCE_LIMIT
    assert result.evidence.transcript_sha256 is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("request_id", "other-request"),
        ("provider_id", "other-provider"),
        ("model", "other-model"),
    ],
)
def test_response_identity_mismatch_fails_closed(field: str, replacement: str) -> None:
    request = _request()
    values = {
        "request_id": request.request_id,
        "provider_id": request.provider_id,
        "model": request.model,
    }
    values[field] = replacement
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=values["request_id"],
            provider_id=values["provider_id"],
            model=values["model"],
            text="untrusted",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR


def test_unavailable_adapter_is_explicit_and_has_no_fallback() -> None:
    result = asyncio.run(
        SpeechToTextService(UnavailableSpeechToTextAdapter()).transcribe(_request())
    )

    assert result.text is None
    assert result.evidence.status is SpeechToTextStatus.UNAVAILABLE
    assert result.evidence.error_code is SpeechToTextFailureCode.UNAVAILABLE
    assert result.evidence.retryable is False


def test_typed_failure_keeps_only_typed_outcome() -> None:
    result = asyncio.run(
        SpeechToTextService(
            _TypedFailureAdapter(SpeechToTextFailureCode.INVALID_AUDIO, retryable=False)
        ).transcribe(_request())
    )

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.INVALID_AUDIO
    assert "diagnostic" not in repr(result.evidence.as_dict())


def test_unknown_adapter_exception_becomes_provider_error_without_diagnostic() -> None:
    result = asyncio.run(SpeechToTextService(_ExplodingAdapter()).transcribe(_request()))

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert result.evidence.retryable is False
    assert "diagnostic" not in repr(result.evidence.as_dict())


def test_timeout_is_bounded_and_reported_without_transcript() -> None:
    request = _request(policy=SpeechToTextPolicy(timeout_seconds=0.01))
    adapter = _BlockedAdapter()

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.TIMEOUT
    assert result.evidence.retryable is True
    assert result.evidence.transcript_sha256 is None


def test_caller_cancellation_propagates_instead_of_fabricating_evidence() -> None:
    async def scenario() -> None:
        request = _request()
        adapter = _BlockedAdapter()
        task = asyncio.create_task(SpeechToTextService(adapter).transcribe(request))
        await adapter.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_invalid_detected_language_and_latency_fail_closed() -> None:
    request = _request()
    bad_language = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="valid transcript",
            detected_language="uk\nforged",
        )
    )
    language_result = asyncio.run(SpeechToTextService(bad_language).transcribe(request))
    assert language_result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR

    too_long_language = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="valid transcript",
            detected_language="en-" + "aa-" * 21 + "aa",
        )
    )
    long_language_result = asyncio.run(
        SpeechToTextService(too_long_language).transcribe(request)
    )
    assert long_language_result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR

    bad_latency = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="valid transcript",
            latency_ms=float("nan"),
        )
    )
    latency_result = asyncio.run(SpeechToTextService(bad_latency).transcribe(request))
    assert latency_result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR


def test_request_and_audio_validation_fail_closed() -> None:
    with pytest.raises(ValueError):
        _request(audio=SpeechAudio(b"x", SpeechAudioFormat.WAV, 7_999, 1))
    with pytest.raises(ValueError):
        SpeechToTextRequest(
            request_id="bad id",
            provider_id="local-stt",
            model="uk-small-v1",
            audio=_audio(),
        )
    with pytest.raises(ValueError):
        SpeechToTextRequest(
            request_id="stt-1",
            provider_id="local-stt",
            model="uk-small-v1",
            audio=_audio(),
            language="uk\nforged",
        )
    with pytest.raises(ValueError):
        SpeechToTextRequest(
            request_id="stt-1",
            provider_id="local-stt",
            model="uk-small-v1",
            audio=_audio(),
            language="en-" + "aa-" * 21 + "aa",
        )
