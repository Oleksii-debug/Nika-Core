from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from nika_core.microphone_capture import (
    MicrophoneCaptureEvidence,
    MicrophoneCaptureRequest,
    MicrophoneCaptureService,
    MicrophoneCaptureStatus,
)
from nika_core.model_gateway.contracts import PrivacyClass
from nika_core.sherpa_onnx_stt import SherpaOnnxWhisperSpeechToTextAdapter
from nika_core.speech_to_text import (
    SpeechAudio,
    SpeechAudioFormat,
    SpeechToTextEvidence,
    SpeechToTextPolicy,
    SpeechToTextRequest,
    SpeechToTextService,
    SpeechToTextStatus,
)
from nika_core.voice_activity import VoiceActivityConfig, VoiceActivityDetector
from nika_core.wake_activation import (
    MAX_TRANSCRIPT_CHARS,
    WakeActivationDetector,
    WakeActivationError,
    WakeActivationEvidence,
    WakeActivationOutcome,
)
from nika_core.windows_microphone_capture import WindowsWasapiMicrophoneCaptureAdapter


class VoiceTurnStatus(StrEnum):
    COMPLETED = "completed"
    CAPTURE_FAILED = "capture_failed"
    NO_VOICE_ACTIVITY = "no_voice_activity"
    TRANSCRIPTION_FAILED = "transcription_failed"
    INVALID_COMPOSITION = "invalid_composition"


@dataclass(frozen=True, slots=True)
class VoiceTurnRequest:
    request_id: str
    capture: MicrophoneCaptureRequest
    stt_provider_id: str
    stt_model: str
    language: str | None = None
    stt_policy: SpeechToTextPolicy = field(
        default_factory=lambda: SpeechToTextPolicy(
            max_transcript_chars=MAX_TRANSCRIPT_CHARS
        )
    )

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or not self.request_id:
            raise TypeError("request_id must be exact non-empty text")
        if type(self.capture) is not MicrophoneCaptureRequest:
            raise TypeError("capture must be an exact MicrophoneCaptureRequest")
        if type(self.capture.request_id) is not str:
            raise TypeError("capture.request_id must be exact text")
        if self.capture.request_id != self.request_id:
            raise ValueError("capture request_id must match voice turn request_id")
        if type(self.stt_provider_id) is not str or not self.stt_provider_id:
            raise TypeError("stt_provider_id must be exact non-empty text")
        if type(self.stt_model) is not str or not self.stt_model:
            raise TypeError("stt_model must be exact non-empty text")
        if self.language is not None and type(self.language) is not str:
            raise TypeError("language must be exact text or None")
        if type(self.stt_policy) is not SpeechToTextPolicy:
            raise TypeError("stt_policy must be an exact SpeechToTextPolicy")
        max_transcript_chars = self.stt_policy.max_transcript_chars
        if type(max_transcript_chars) is not int:
            raise TypeError("stt_policy.max_transcript_chars must be an exact integer")
        if max_transcript_chars > MAX_TRANSCRIPT_CHARS:
            raise ValueError(
                "stt_policy.max_transcript_chars exceeds wake activation bound"
            )
        sample_count = self.capture.sample_count
        max_audio_bytes = self.stt_policy.max_audio_bytes
        if type(sample_count) is not int or type(max_audio_bytes) is not int:
            raise TypeError("voice turn audio bounds must be exact integers")
        if sample_count * 2 > max_audio_bytes:
            raise ValueError("capture audio exceeds stt_policy.max_audio_bytes")


@dataclass(frozen=True, slots=True)
class VoiceTurnEvidence:
    request_id: str
    status: VoiceTurnStatus
    capture: MicrophoneCaptureEvidence
    transcription: SpeechToTextEvidence | None
    wake: WakeActivationEvidence | None
    activated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.voice-turn-evidence:v1",
            "request_id": self.request_id,
            "status": self.status.value,
            "capture": self.capture.as_dict(),
            "transcription": (
                self.transcription.as_dict() if self.transcription is not None else None
            ),
            "wake": (
                {
                    "request_id": self.wake.request_id,
                    "outcome": self.wake.outcome.value,
                    "transcript_sha256": self.wake.transcript_sha256,
                    "token_count": self.wake.token_count,
                    "matched_phrase_sha256": self.wake.matched_phrase_sha256,
                    "match_start_token": self.wake.match_start_token,
                    "match_end_token_exclusive": self.wake.match_end_token_exclusive,
                }
                if self.wake is not None
                else None
            ),
            "activated": self.activated,
        }


@dataclass(frozen=True, slots=True)
class VoiceTurnResult:
    transcript: str | None
    evidence: VoiceTurnEvidence


class OneShotVoiceTurnService:
    """Explicitly invoked local microphone -> optional VAD -> STT -> wake composition.

    This service has no loop, scheduler, persistence or privileged-action authority.
    Each call represents one caller-authorized microphone turn.
    """

    def __init__(
        self,
        *,
        microphone: MicrophoneCaptureService,
        speech_to_text: SpeechToTextService,
        wake_detector: WakeActivationDetector,
        enable_voice_activity: bool = False,
    ) -> None:
        if type(microphone) is not MicrophoneCaptureService:
            raise TypeError("microphone must be an exact MicrophoneCaptureService")
        if type(speech_to_text) is not SpeechToTextService:
            raise TypeError("speech_to_text must be an exact SpeechToTextService")
        if type(wake_detector) is not WakeActivationDetector:
            raise TypeError("wake_detector must be an exact WakeActivationDetector")
        if type(enable_voice_activity) is not bool:
            raise TypeError("enable_voice_activity must be an exact bool")
        self._microphone = microphone
        self._speech_to_text = speech_to_text
        self._wake_detector = wake_detector
        self._enable_voice_activity = enable_voice_activity

    async def run(self, request: VoiceTurnRequest) -> VoiceTurnResult:
        trusted = _snapshot_request(request)
        stt_authority = _preflight_stt_authority(trusted)
        capture_result = await self._microphone.capture(trusted.capture)
        if (
            capture_result.evidence.status is not MicrophoneCaptureStatus.SUCCEEDED
            or capture_result.pcm_s16le is None
        ):
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.CAPTURE_FAILED,
                    capture=capture_result.evidence,
                    transcription=None,
                    wake=None,
                    activated=False,
                ),
            )

        if self._enable_voice_activity and not _has_sustained_voice_activity(
            capture_result.pcm_s16le,
            sample_rate_hz=trusted.capture.sample_rate_hz,
        ):
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.NO_VOICE_ACTIVITY,
                    capture=capture_result.evidence,
                    transcription=None,
                    wake=None,
                    activated=False,
                ),
            )

        audio = SpeechAudio(
            data=capture_result.pcm_s16le,
            audio_format=SpeechAudioFormat.PCM_S16LE,
            sample_rate_hz=trusted.capture.sample_rate_hz,
            channels=1,
        )
        stt_request = SpeechToTextRequest(
            request_id=stt_authority.request_id,
            provider_id=stt_authority.provider_id,
            model=stt_authority.model,
            audio=audio,
            language=stt_authority.language,
            privacy=stt_authority.privacy,
            policy=stt_authority.policy,
        )
        transcription = await self._speech_to_text.transcribe(stt_request)
        if (
            transcription.evidence.status is not SpeechToTextStatus.SUCCEEDED
            or transcription.text is None
        ):
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.TRANSCRIPTION_FAILED,
                    capture=capture_result.evidence,
                    transcription=transcription.evidence,
                    wake=None,
                    activated=False,
                ),
            )

        if (
            capture_result.evidence.audio_sha256 is None
            or transcription.evidence.audio_sha256
            != capture_result.evidence.audio_sha256
        ):
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.INVALID_COMPOSITION,
                    capture=capture_result.evidence,
                    transcription=transcription.evidence,
                    wake=None,
                    activated=False,
                ),
            )

        try:
            wake = self._wake_detector.detect(
                request_id=trusted.request_id,
                transcript=transcription.text,
            )
        except WakeActivationError:
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.INVALID_COMPOSITION,
                    capture=capture_result.evidence,
                    transcription=transcription.evidence,
                    wake=None,
                    activated=False,
                ),
            )
        if (
            transcription.evidence.transcript_sha256 is None
            or wake.transcript_sha256 != transcription.evidence.transcript_sha256
        ):
            return VoiceTurnResult(
                transcript=None,
                evidence=VoiceTurnEvidence(
                    request_id=trusted.request_id,
                    status=VoiceTurnStatus.INVALID_COMPOSITION,
                    capture=capture_result.evidence,
                    transcription=transcription.evidence,
                    wake=wake,
                    activated=False,
                ),
            )

        return VoiceTurnResult(
            transcript=transcription.text,
            evidence=VoiceTurnEvidence(
                request_id=trusted.request_id,
                status=VoiceTurnStatus.COMPLETED,
                capture=capture_result.evidence,
                transcription=transcription.evidence,
                wake=wake,
                activated=wake.outcome is WakeActivationOutcome.DETECTED,
            ),
        )


def _snapshot_request(request: VoiceTurnRequest) -> VoiceTurnRequest:
    if type(request) is not VoiceTurnRequest:
        raise TypeError("request must be an exact VoiceTurnRequest")
    capture = MicrophoneCaptureRequest(
        request_id=request.capture.request_id,
        provider_id=request.capture.provider_id,
        device_id=request.capture.device_id,
        sample_rate_hz=request.capture.sample_rate_hz,
        sample_count=request.capture.sample_count,
        policy=request.capture.policy,
    )
    policy = SpeechToTextPolicy(
        max_audio_bytes=request.stt_policy.max_audio_bytes,
        max_transcript_chars=request.stt_policy.max_transcript_chars,
        timeout_seconds=request.stt_policy.timeout_seconds,
    )
    return VoiceTurnRequest(
        request_id=request.request_id,
        capture=capture,
        stt_provider_id=request.stt_provider_id,
        stt_model=request.stt_model,
        language=request.language,
        stt_policy=policy,
    )


def _preflight_stt_authority(request: VoiceTurnRequest) -> SpeechToTextRequest:
    placeholder_audio = SpeechAudio(
        data=b"\x00\x00",
        audio_format=SpeechAudioFormat.PCM_S16LE,
        sample_rate_hz=request.capture.sample_rate_hz,
        channels=1,
    )
    return SpeechToTextRequest(
        request_id=request.request_id,
        provider_id=request.stt_provider_id,
        model=request.stt_model,
        audio=placeholder_audio,
        language=request.language,
        privacy=PrivacyClass.SENSITIVE,
        policy=request.stt_policy,
    )


def _has_sustained_voice_activity(
    pcm_s16le: bytes,
    *,
    sample_rate_hz: int,
) -> bool:
    config = VoiceActivityConfig(sample_rate_hz=sample_rate_hz)
    detector = VoiceActivityDetector(config)
    frame_bytes = config.max_frame_bytes
    for offset in range(0, len(pcm_s16le), frame_bytes):
        decision = detector.process(pcm_s16le[offset : offset + frame_bytes])
        if decision.speech_active:
            return True
    return False


def build_windows_one_shot_voice_turn_service(
    *,
    encoder: str,
    decoder: str,
    tokens: str,
    model_id: str,
    language: str,
    num_threads: int = 2,
    sounddevice_module: object | None = None,
    sherpa_module: object | None = None,
) -> OneShotVoiceTurnService:
    """Compose the real local Windows capture and sherpa STT backends.

    Model files must already exist locally; this factory performs no download,
    network access, persistence, background listening or privileged action.
    """
    microphone_adapter = WindowsWasapiMicrophoneCaptureAdapter(
        sounddevice_module=sounddevice_module,
    )
    stt_adapter = SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
        encoder=encoder,
        decoder=decoder,
        tokens=tokens,
        model_id=model_id,
        language=language,
        num_threads=num_threads,
        sherpa_module=sherpa_module,
    )
    return OneShotVoiceTurnService(
        microphone=MicrophoneCaptureService(microphone_adapter),
        speech_to_text=SpeechToTextService(stt_adapter),
        wake_detector=WakeActivationDetector(),
        enable_voice_activity=True,
    )
