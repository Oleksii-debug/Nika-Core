from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from nika_core.microphone_capture import MicrophoneCaptureEvidence, MicrophoneCaptureStatus
from nika_core.speaker_verification import (
    SpeakerVerificationEvidence,
    SpeakerVerificationOutcome,
)
from nika_core.speech_to_text import SpeechToTextEvidence, SpeechToTextStatus
from nika_core.voice_turn import VoiceTurnEvidence, VoiceTurnStatus
from nika_core.wake_activation import WakeActivationEvidence, WakeActivationOutcome

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class VoiceAuthorizationError(ValueError):
    """Purported voice evidence is malformed or internally inconsistent."""


class VoiceAuthorizationDecision(StrEnum):
    OWNER_AUTHORIZED = "owner_authorized"
    CONFIRMATION_REQUIRED = "confirmation_required"
    DENIED = "denied"


class VoiceAuthorizationReason(StrEnum):
    OWNER_MATCH = "owner_match"
    SPEAKER_UNCERTAIN = "speaker_uncertain"
    SPEAKER_NO_MATCH = "speaker_no_match"
    WAKE_NOT_DETECTED = "wake_not_detected"


@dataclass(frozen=True, slots=True)
class VoiceAuthorizationEvidence:
    request_id: str
    decision: VoiceAuthorizationDecision
    reason: VoiceAuthorizationReason
    capture_audio_sha256: str
    transcript_sha256: str
    speaker_profile_id_sha256: str
    speaker_profile_revision_sha256: str
    speaker_provider_id: str
    speaker_model_id: str
    speaker_confidence: float

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika.voice-authorization-evidence:v1",
            "request_id": self.request_id,
            "decision": self.decision.value,
            "reason": self.reason.value,
            "capture_audio_sha256": self.capture_audio_sha256,
            "transcript_sha256": self.transcript_sha256,
            "speaker_profile_id_sha256": self.speaker_profile_id_sha256,
            "speaker_profile_revision_sha256": self.speaker_profile_revision_sha256,
            "speaker_provider_id": self.speaker_provider_id,
            "speaker_model_id": self.speaker_model_id,
            "speaker_confidence": self.speaker_confidence,
        }


@dataclass(frozen=True, slots=True)
class _VoiceSnapshot:
    request_id: str
    audio_sha256: str
    audio_byte_count: int
    sample_rate_hz: int
    transcript_sha256: str
    wake_outcome: WakeActivationOutcome


@dataclass(frozen=True, slots=True)
class _SpeakerSnapshot:
    request_id: str
    provider_id: str
    model_id: str
    profile_id_sha256: str
    profile_revision_sha256: str
    audio_sha256: str
    audio_byte_count: int
    sample_rate_hz: int
    confidence: float
    outcome: SpeakerVerificationOutcome


class OwnerVoiceAuthorizationGate:
    """Pure wake + speaker-confidence admission policy with no execution authority."""

    def evaluate(
        self,
        *,
        voice_turn: VoiceTurnEvidence,
        speaker: SpeakerVerificationEvidence,
    ) -> VoiceAuthorizationEvidence:
        voice = _snapshot_voice_turn(voice_turn)
        verified_speaker = _snapshot_speaker(speaker)

        if verified_speaker.request_id != voice.request_id:
            raise VoiceAuthorizationError(
                "speaker verification request_id does not match voice turn"
            )
        if verified_speaker.audio_sha256 != voice.audio_sha256:
            raise VoiceAuthorizationError(
                "speaker verification audio does not match captured voice turn"
            )
        if verified_speaker.audio_byte_count != voice.audio_byte_count:
            raise VoiceAuthorizationError(
                "speaker verification audio length does not match captured voice turn"
            )
        if verified_speaker.sample_rate_hz != voice.sample_rate_hz:
            raise VoiceAuthorizationError(
                "speaker verification sample rate does not match captured voice turn"
            )

        if voice.wake_outcome is WakeActivationOutcome.NOT_DETECTED:
            decision = VoiceAuthorizationDecision.DENIED
            reason = VoiceAuthorizationReason.WAKE_NOT_DETECTED
        elif verified_speaker.outcome is SpeakerVerificationOutcome.MATCH:
            decision = VoiceAuthorizationDecision.OWNER_AUTHORIZED
            reason = VoiceAuthorizationReason.OWNER_MATCH
        elif verified_speaker.outcome is SpeakerVerificationOutcome.UNCERTAIN:
            decision = VoiceAuthorizationDecision.CONFIRMATION_REQUIRED
            reason = VoiceAuthorizationReason.SPEAKER_UNCERTAIN
        else:
            decision = VoiceAuthorizationDecision.DENIED
            reason = VoiceAuthorizationReason.SPEAKER_NO_MATCH

        return VoiceAuthorizationEvidence(
            request_id=voice.request_id,
            decision=decision,
            reason=reason,
            capture_audio_sha256=voice.audio_sha256,
            transcript_sha256=voice.transcript_sha256,
            speaker_profile_id_sha256=verified_speaker.profile_id_sha256,
            speaker_profile_revision_sha256=verified_speaker.profile_revision_sha256,
            speaker_provider_id=verified_speaker.provider_id,
            speaker_model_id=verified_speaker.model_id,
            speaker_confidence=verified_speaker.confidence,
        )


def _snapshot_voice_turn(value: object) -> _VoiceSnapshot:
    if type(value) is not VoiceTurnEvidence:
        raise VoiceAuthorizationError("voice_turn must use canonical VoiceTurnEvidence")
    if type(value.status) is not VoiceTurnStatus or value.status is not VoiceTurnStatus.COMPLETED:
        raise VoiceAuthorizationError("voice turn must be successfully completed")
    if type(value.activated) is not bool:
        raise VoiceAuthorizationError("voice turn activated flag must be exact bool")
    if type(value.capture) is not MicrophoneCaptureEvidence:
        raise VoiceAuthorizationError("voice turn capture evidence is not canonical")
    if type(value.transcription) is not SpeechToTextEvidence:
        raise VoiceAuthorizationError("completed voice turn must include canonical STT evidence")
    if type(value.wake) is not WakeActivationEvidence:
        raise VoiceAuthorizationError("completed voice turn must include canonical wake evidence")

    request_id = _safe_id(value.request_id, field="voice turn request_id")
    capture = value.capture
    transcription = value.transcription
    wake = value.wake

    if (
        _safe_id(capture.request_id, field="capture request_id") != request_id
        or _safe_id(transcription.request_id, field="transcription request_id") != request_id
        or _safe_id(wake.request_id, field="wake request_id") != request_id
    ):
        raise VoiceAuthorizationError("voice turn evidence request identities do not agree")
    if (
        type(capture.status) is not MicrophoneCaptureStatus
        or capture.status is not MicrophoneCaptureStatus.SUCCEEDED
    ):
        raise VoiceAuthorizationError("completed voice turn requires successful capture")
    if (
        type(transcription.status) is not SpeechToTextStatus
        or transcription.status is not SpeechToTextStatus.SUCCEEDED
    ):
        raise VoiceAuthorizationError("completed voice turn requires successful transcription")
    if type(wake.outcome) is not WakeActivationOutcome:
        raise VoiceAuthorizationError("wake outcome must use canonical enum")

    audio_sha256 = _sha256(capture.audio_sha256, field="capture audio_sha256")
    transcription_audio_sha256 = _sha256(
        transcription.audio_sha256, field="transcription audio_sha256"
    )
    transcript_sha256 = _sha256(
        transcription.transcript_sha256, field="transcription transcript_sha256"
    )
    wake_transcript_sha256 = _sha256(wake.transcript_sha256, field="wake transcript_sha256")
    audio_byte_count = _positive_int(capture.audio_byte_count, field="capture audio_byte_count")
    sample_count = _positive_int(capture.sample_count, field="capture sample_count")
    sample_rate_hz = _positive_int(capture.sample_rate_hz, field="capture sample_rate_hz")
    transcription_audio_bytes = _positive_int(
        transcription.audio_bytes, field="transcription audio_bytes"
    )
    transcription_sample_rate = _positive_int(
        transcription.sample_rate_hz, field="transcription sample_rate_hz"
    )

    if audio_byte_count != sample_count * 2:
        raise VoiceAuthorizationError("capture PCM16 byte count is inconsistent")
    if type(transcription.channels) is not int or transcription.channels != 1:
        raise VoiceAuthorizationError("voice turn transcription must remain exact mono")
    if (
        transcription_audio_sha256 != audio_sha256
        or transcription_audio_bytes != audio_byte_count
        or transcription_sample_rate != sample_rate_hz
    ):
        raise VoiceAuthorizationError("transcription evidence does not bind captured audio")
    if wake_transcript_sha256 != transcript_sha256:
        raise VoiceAuthorizationError("wake evidence does not bind transcription")

    token_count = wake.token_count
    if type(token_count) is not int or token_count < 0:
        raise VoiceAuthorizationError("wake token_count must be nonnegative exact integer")
    if wake.outcome is WakeActivationOutcome.DETECTED:
        _sha256(wake.matched_phrase_sha256, field="matched wake phrase sha256")
        if (
            type(wake.match_start_token) is not int
            or type(wake.match_end_token_exclusive) is not int
            or wake.match_start_token < 0
            or wake.match_end_token_exclusive <= wake.match_start_token
            or wake.match_end_token_exclusive > token_count
        ):
            raise VoiceAuthorizationError("detected wake token range is invalid")
    elif (
        wake.matched_phrase_sha256 is not None
        or wake.match_start_token is not None
        or wake.match_end_token_exclusive is not None
    ):
        raise VoiceAuthorizationError(
            "non-detected wake must not carry matched phrase evidence"
        )

    if value.activated is not (wake.outcome is WakeActivationOutcome.DETECTED):
        raise VoiceAuthorizationError(
            "voice turn activated flag does not match wake activation outcome"
        )

    return _VoiceSnapshot(
        request_id=request_id,
        audio_sha256=audio_sha256,
        audio_byte_count=audio_byte_count,
        sample_rate_hz=sample_rate_hz,
        transcript_sha256=transcript_sha256,
        wake_outcome=wake.outcome,
    )


def _snapshot_speaker(value: object) -> _SpeakerSnapshot:
    if type(value) is not SpeakerVerificationEvidence:
        raise VoiceAuthorizationError(
            "speaker must use canonical SpeakerVerificationEvidence"
        )
    request_id = _safe_id(value.request_id, field="speaker request_id")
    provider_id = _safe_id(value.provider_id, field="speaker provider_id")
    model_id = _safe_id(value.model_id, field="speaker model_id")
    profile_id_sha256 = _sha256(value.profile_id_sha256, field="speaker profile_id_sha256")
    profile_revision_sha256 = _sha256(
        value.profile_revision_sha256, field="speaker profile_revision_sha256"
    )
    audio_sha256 = _sha256(value.audio_sha256, field="speaker audio_sha256")
    audio_byte_count = _positive_int(
        value.audio_byte_count, field="speaker audio_byte_count"
    )
    sample_rate_hz = _positive_int(value.sample_rate_hz, field="speaker sample_rate_hz")
    if audio_byte_count % 2:
        raise VoiceAuthorizationError("speaker audio must contain complete PCM16 samples")
    if (
        type(value.duration_seconds) not in (int, float)
        or isinstance(value.duration_seconds, bool)
        or not math.isfinite(float(value.duration_seconds))
        or not math.isclose(
            float(value.duration_seconds),
            audio_byte_count / (sample_rate_hz * 2),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        raise VoiceAuthorizationError("speaker duration does not match audio geometry")
    if (
        type(value.confidence) not in (int, float)
        or isinstance(value.confidence, bool)
        or not math.isfinite(float(value.confidence))
        or not 0.0 <= float(value.confidence) <= 1.0
    ):
        raise VoiceAuthorizationError("speaker confidence must be finite in [0, 1]")
    if type(value.outcome) is not SpeakerVerificationOutcome:
        raise VoiceAuthorizationError("speaker outcome must use canonical enum")

    return _SpeakerSnapshot(
        request_id=request_id,
        provider_id=provider_id,
        model_id=model_id,
        profile_id_sha256=profile_id_sha256,
        profile_revision_sha256=profile_revision_sha256,
        audio_sha256=audio_sha256,
        audio_byte_count=audio_byte_count,
        sample_rate_hz=sample_rate_hz,
        confidence=float(value.confidence),
        outcome=value.outcome,
    )


def _safe_id(value: object, *, field: str) -> str:
    if type(value) is not str or not _SAFE_ID_RE.fullmatch(value):
        raise VoiceAuthorizationError(f"{field} must be a bounded safe identifier")
    return value


def _sha256(value: object, *, field: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise VoiceAuthorizationError(f"{field} must be lowercase sha256")
    return value


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise VoiceAuthorizationError(f"{field} must be a positive exact integer")
    return value
