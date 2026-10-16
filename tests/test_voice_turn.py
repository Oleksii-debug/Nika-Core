from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from nika_core.microphone_capture import (
    MicrophoneCaptureAdapterError,
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureFailureCode,
    MicrophoneCapturePolicy,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
    MicrophoneCaptureService,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.speech_to_text import (
    SpeechToTextAdapterResponse,
    SpeechToTextPolicy,
    SpeechToTextRequest,
    SpeechToTextService,
)
from nika_core.voice_turn import (
    OneShotVoiceTurnService,
    VoiceTurnRequest,
    VoiceTurnStatus,
    build_windows_one_shot_voice_turn_service,
)
from nika_core.wake_activation import MAX_TRANSCRIPT_CHARS, WakeActivationDetector


class _MicrophoneAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id="sounddevice-wasapi",
            device_id="wasapi-device-sha256:" + "a" * 64,
        )

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.calls += 1
        if self.fail:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "synthetic microphone unavailable",
                retryable=False,
            )
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=b"\x01\x00" * request.sample_count,
            latency_ms=1.0,
        )


class _SttAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "local-stt"
    supported_models = ("uk-small-v1",)

    def __init__(self, transcript: str = "Ніка, продовжуй") -> None:
        self.transcript = transcript
        self.calls: list[SpeechToTextRequest] = []

    async def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextAdapterResponse:
        self.calls.append(request)
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text=self.transcript,
            detected_language="uk",
            latency_ms=2.0,
        )


def _request() -> VoiceTurnRequest:
    device_id = "wasapi-device-sha256:" + "a" * 64
    return VoiceTurnRequest(
        request_id="voice-turn-1",
        capture=MicrophoneCaptureRequest(
            request_id="voice-turn-1",
            provider_id="sounddevice-wasapi",
            device_id=device_id,
            sample_rate_hz=16_000,
            sample_count=8,
            policy=MicrophoneCapturePolicy(timeout_seconds=1.0),
        ),
        stt_provider_id="local-stt",
        stt_model="uk-small-v1",
        language="uk",
        stt_policy=SpeechToTextPolicy(
            max_audio_bytes=1024,
            max_transcript_chars=MAX_TRANSCRIPT_CHARS,
            timeout_seconds=1.0,
        ),
    )


def _service(
    microphone: _MicrophoneAdapter,
    stt: _SttAdapter,
) -> OneShotVoiceTurnService:
    return OneShotVoiceTurnService(
        microphone=MicrophoneCaptureService(microphone),
        speech_to_text=SpeechToTextService(stt),
        wake_detector=WakeActivationDetector(),
    )


def test_windows_factory_composes_real_backend_types_without_network() -> None:
    class _OfflineRecognizer:
        calls: ClassVar[list[dict[str, object]]] = []

        @classmethod
        def from_whisper(cls, **kwargs):
            cls.calls.append(dict(kwargs))
            return object()

    class _SherpaModule:
        OfflineRecognizer = _OfflineRecognizer

    service = build_windows_one_shot_voice_turn_service(
        encoder=r"C:\models\encoder.onnx",
        decoder=r"C:\models\decoder.onnx",
        tokens=r"C:\models\tokens.txt",
        model_id="whisper-uk-v1",
        language="uk",
        num_threads=3,
        sounddevice_module=_MicrophoneAdapter(),
        sherpa_module=_SherpaModule,
    )

    assert type(service) is OneShotVoiceTurnService
    assert _OfflineRecognizer.calls == [
        {
            "encoder": r"C:\models\encoder.onnx",
            "decoder": r"C:\models\decoder.onnx",
            "tokens": r"C:\models\tokens.txt",
            "language": "uk",
            "task": "transcribe",
            "num_threads": 3,
            "debug": False,
            "provider": "cpu",
        }
    ]


def test_one_shot_turn_composes_capture_stt_and_wake_without_durable_content() -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter("Ніка, продовжуй")
    result = asyncio.run(_service(microphone, stt).run(_request()))

    assert result.evidence.status is VoiceTurnStatus.COMPLETED
    assert result.evidence.activated is True
    assert result.transcript == "Ніка, продовжуй"
    assert microphone.calls == 1
    assert len(stt.calls) == 1

    durable = result.evidence.as_dict()
    rendered = repr(durable)
    assert "Ніка, продовжуй" not in rendered
    assert "\\x01\\x00" not in rendered
    assert durable["capture"]["audio_sha256"] == durable["transcription"]["audio_sha256"]
    assert (
        durable["transcription"]["transcript_sha256"]
        == durable["wake"]["transcript_sha256"]
    )


def test_capture_failure_stops_before_stt_and_wake() -> None:
    microphone = _MicrophoneAdapter(fail=True)
    stt = _SttAdapter()
    result = asyncio.run(_service(microphone, stt).run(_request()))

    assert result.evidence.status is VoiceTurnStatus.CAPTURE_FAILED
    assert result.transcript is None
    assert result.evidence.transcription is None
    assert result.evidence.wake is None
    assert result.evidence.activated is False
    assert stt.calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("stt_provider_id", "bad provider", "provider_id"),
        ("stt_model", "bad model", "model"),
        ("language", "not a valid language tag", "language"),
    ),
)
def test_malformed_stt_authority_fails_before_microphone_effect(
    field: str,
    value: str,
    message: str,
) -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter()
    request = _request()
    object.__setattr__(request, field, value)

    with pytest.raises(ValueError, match=message):
        asyncio.run(_service(microphone, stt).run(request))

    assert microphone.calls == 0
    assert stt.calls == []


def test_impossible_stt_audio_budget_fails_before_microphone_effect() -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter()
    request = _request()
    object.__setattr__(request.stt_policy, "max_audio_bytes", 2)

    with pytest.raises(ValueError, match="max_audio_bytes"):
        asyncio.run(_service(microphone, stt).run(request))

    assert microphone.calls == 0
    assert stt.calls == []


def test_stt_route_failure_stops_before_wake() -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter()
    request = _request()
    object.__setattr__(request, "stt_model", "wrong-model")

    result = asyncio.run(_service(microphone, stt).run(request))

    assert result.evidence.status is VoiceTurnStatus.TRANSCRIPTION_FAILED
    assert result.transcript is None
    assert result.evidence.wake is None
    assert result.evidence.activated is False
    assert stt.calls == []


def test_non_wake_transcript_completes_without_activation() -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter("сьогодні гарна погода")
    result = asyncio.run(_service(microphone, stt).run(_request()))

    assert result.evidence.status is VoiceTurnStatus.COMPLETED
    assert result.evidence.activated is False
    assert result.transcript == "сьогодні гарна погода"


def test_wake_rejection_fails_closed_without_exposing_transcript() -> None:
    microphone = _MicrophoneAdapter()
    stt = _SttAdapter("Ніка\x00секрет")
    result = asyncio.run(_service(microphone, stt).run(_request()))

    assert result.evidence.status is VoiceTurnStatus.INVALID_COMPOSITION
    assert result.transcript is None
    assert result.evidence.wake is None
    assert result.evidence.activated is False
    assert microphone.calls == 1
    assert len(stt.calls) == 1
    rendered = repr(result.evidence.as_dict())
    assert "Ніка" not in rendered
    assert "секрет" not in rendered


def test_caller_mutation_after_turn_start_cannot_retarget_stt_authority() -> None:
    class _HeldMicrophone(_MicrophoneAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def capture(
            self,
            request: MicrophoneCaptureRequest,
        ) -> MicrophoneCaptureResponse:
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return MicrophoneCaptureResponse(
                request_id=request.request_id,
                provider_id=request.provider_id,
                device_id=request.device_id,
                sample_rate_hz=request.sample_rate_hz,
                pcm_s16le=b"\x01\x00" * request.sample_count,
                latency_ms=1.0,
            )

    async def scenario() -> None:
        microphone = _HeldMicrophone()
        stt = _SttAdapter()
        request = _request()
        task = asyncio.create_task(_service(microphone, stt).run(request))
        await microphone.entered.wait()

        object.__setattr__(request, "stt_provider_id", "forged-provider")
        object.__setattr__(request, "stt_model", "forged-model")
        object.__setattr__(request, "language", "en")
        object.__setattr__(
            request.stt_policy,
            "max_transcript_chars",
            1,
        )
        microphone.release.set()

        result = await task

        assert result.evidence.status is VoiceTurnStatus.COMPLETED
        assert result.evidence.activated is True
        assert len(stt.calls) == 1
        assert stt.calls[0].provider_id == "local-stt"
        assert stt.calls[0].model == "uk-small-v1"
        assert stt.calls[0].language == "uk"
        assert stt.calls[0].policy.max_transcript_chars == MAX_TRANSCRIPT_CHARS

    asyncio.run(scenario())


def test_turn_rejects_stt_transcript_bound_larger_than_wake_authority() -> None:
    request = _request()
    with pytest.raises(ValueError, match="exceeds wake activation bound"):
        VoiceTurnRequest(
            request_id=request.request_id,
            capture=request.capture,
            stt_provider_id=request.stt_provider_id,
            stt_model=request.stt_model,
            language=request.language,
            stt_policy=SpeechToTextPolicy(
                max_transcript_chars=MAX_TRANSCRIPT_CHARS + 1
            ),
        )


def test_turn_requires_one_exact_request_identity_across_capture_and_composition() -> None:
    base = _request()
    with pytest.raises(ValueError, match="capture request_id"):
        VoiceTurnRequest(
            request_id="voice-turn-other",
            capture=base.capture,
            stt_provider_id=base.stt_provider_id,
            stt_model=base.stt_model,
        )


def test_turn_rejects_behavioral_nested_request_id_without_dispatch() -> None:
    class BehavioralStr(str):
        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("nested request identity comparison must not execute")

    base = _request()
    object.__setattr__(base.capture, "request_id", BehavioralStr(base.request_id))

    with pytest.raises(TypeError, match="capture.request_id"):
        VoiceTurnRequest(
            request_id=base.request_id,
            capture=base.capture,
            stt_provider_id=base.stt_provider_id,
            stt_model=base.stt_model,
            stt_policy=base.stt_policy,
        )


def test_turn_rejects_behavioral_nested_audio_bounds_without_dispatch() -> None:
    class BehavioralInt(int):
        def __gt__(self, other: object) -> bool:
            del other
            raise AssertionError("nested numeric comparison must not execute")

        def __mul__(self, other: object) -> int:
            del other
            raise AssertionError("nested numeric multiplication must not execute")

    base = _request()
    object.__setattr__(base.capture, "sample_count", BehavioralInt(8))

    with pytest.raises(TypeError, match="audio bounds"):
        VoiceTurnRequest(
            request_id=base.request_id,
            capture=base.capture,
            stt_provider_id=base.stt_provider_id,
            stt_model=base.stt_model,
            stt_policy=base.stt_policy,
        )


def test_turn_rejects_behavioral_transcript_bound_without_dispatch() -> None:
    class BehavioralInt(int):
        def __gt__(self, other: object) -> bool:
            del other
            raise AssertionError("transcript bound comparison must not execute")

    base = _request()
    object.__setattr__(
        base.stt_policy,
        "max_transcript_chars",
        BehavioralInt(MAX_TRANSCRIPT_CHARS),
    )

    with pytest.raises(TypeError, match="max_transcript_chars"):
        VoiceTurnRequest(
            request_id=base.request_id,
            capture=base.capture,
            stt_provider_id=base.stt_provider_id,
            stt_model=base.stt_model,
            stt_policy=base.stt_policy,
        )


def test_caller_cancellation_propagates_without_fabricated_turn_evidence() -> None:
    class _BlockedStt(_SttAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def transcribe(
            self,
            request: SpeechToTextRequest,
        ) -> SpeechToTextAdapterResponse:
            self.calls.append(request)
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        microphone = _MicrophoneAdapter()
        stt = _BlockedStt()
        task = asyncio.create_task(_service(microphone, stt).run(_request()))
        await stt.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
