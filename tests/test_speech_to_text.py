from __future__ import annotations

import asyncio
from typing import ClassVar

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
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

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
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        del request
        raise RuntimeError("synthetic provider diagnostic must not escape")


class _TypedFailureAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

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
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

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


class _MalformedModelRouteAdapter(_RecordingAdapter):
    supported_models: ClassVar[list[str]] = ["uk-small-v1"]


class _MalformedTypedFailureAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        del request
        raise SpeechToTextAdapterError(  # type: ignore[arg-type]
            "provider_error",
            "synthetic malformed typed failure",
            retryable=1,  # type: ignore[arg-type]
        )


class _MutatingIdentityAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        self.calls += 1
        object.__setattr__(request, "request_id", "forged-request")
        object.__setattr__(request, "model", "forged-model")
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="forged identity",
        )


class _MutatingEvidenceAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        self.calls += 1
        object.__setattr__(request.policy, "max_transcript_chars", 100)
        object.__setattr__(request, "privacy", PrivacyClass.PRIVATE)
        object.__setattr__(request.audio, "data", b"tampered-audio")
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="four",
        )


class _BehavioralText(str):
    def strip(self, *_args: object, **_kwargs: object) -> str:
        return "forged transcript"


class _BehavioralFloat(float):
    def __float__(self) -> float:
        return 0.0


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
    provider_id: str = "local-stt",
    model: str = "uk-small-v1",
) -> SpeechToTextRequest:
    return SpeechToTextRequest(
        request_id="stt-1",
        provider_id=provider_id,
        model=model,
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


def test_wrong_provider_route_is_rejected_before_audio_is_sent() -> None:
    request = _request(provider_id="other-local-stt")
    adapter = _RecordingAdapter(
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


def test_wrong_model_route_is_rejected_before_audio_is_sent() -> None:
    request = _request(model="unconfigured-model")
    adapter = _RecordingAdapter(
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


def test_malformed_model_capability_is_rejected_before_audio_is_sent() -> None:
    request = _request()
    adapter = _MalformedModelRouteAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="must not run",
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))  # type: ignore[arg-type]

    assert result.text is None
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


def test_adapter_cannot_retarget_admitted_request_identity() -> None:
    request = _request()
    adapter = _MutatingIdentityAdapter()

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert adapter.calls == 1
    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert result.evidence.request_id == "stt-1"
    assert result.evidence.model == "uk-small-v1"
    assert request.request_id == "stt-1"
    assert request.model == "uk-small-v1"


def test_adapter_cannot_mutate_policy_privacy_or_audio_evidence() -> None:
    request = _request(policy=SpeechToTextPolicy(max_transcript_chars=3))
    original_audio = request.audio.data
    adapter = _MutatingEvidenceAdapter()

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert adapter.calls == 1
    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.RESOURCE_LIMIT
    assert result.evidence.privacy is PrivacyClass.SENSITIVE
    assert result.evidence.audio_bytes == len(original_audio)
    assert request.policy.max_transcript_chars == 3
    assert request.privacy is PrivacyClass.SENSITIVE
    assert request.audio.data == original_audio


def test_post_init_forged_audio_is_rejected_before_adapter_dispatch() -> None:
    request = _request()
    object.__setattr__(request.audio, "data", bytearray(b"forged-audio"))
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id="stt-1",
            provider_id="local-stt",
            model="uk-small-v1",
            text="must not run",
        )
    )

    with pytest.raises(ValueError, match="audio data must be non-empty bytes"):
        asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert adapter.calls == []


def test_behavioral_transcript_carrier_is_rejected() -> None:
    request = _request()
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text=_BehavioralText("forged transcript"),
        )
    )

    result = asyncio.run(SpeechToTextService(adapter).transcribe(request))

    assert result.text is None
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR


def test_behavioral_latency_carrier_is_rejected() -> None:
    request = _request()
    adapter = _RecordingAdapter(
        SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text="valid transcript",
            latency_ms=_BehavioralFloat(1.0),
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


def test_malformed_typed_failure_becomes_redacted_provider_error() -> None:
    result = asyncio.run(
        SpeechToTextService(_MalformedTypedFailureAdapter()).transcribe(_request())
    )

    assert result.text is None
    assert result.evidence.status is SpeechToTextStatus.FAILED
    assert result.evidence.error_code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert result.evidence.retryable is False
    assert result.evidence.as_dict()["error_code"] == "provider_error"


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
