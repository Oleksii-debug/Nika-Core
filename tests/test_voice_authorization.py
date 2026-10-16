from __future__ import annotations

import dataclasses
import hashlib

import pytest

from nika_core.microphone_capture import MicrophoneCaptureEvidence, MicrophoneCaptureStatus
from nika_core.model_gateway.contracts import PrivacyClass
from nika_core.speaker_verification import (
    SpeakerVerificationEvidence,
    SpeakerVerificationOutcome,
)
from nika_core.speech_to_text import (
    SpeechAudioFormat,
    SpeechToTextEvidence,
    SpeechToTextStatus,
)
from nika_core.voice_authorization import (
    OwnerVoiceAuthorizationGate,
    VoiceAuthorizationDecision,
    VoiceAuthorizationError,
    VoiceAuthorizationReason,
)
from nika_core.voice_turn import VoiceTurnEvidence, VoiceTurnStatus
from nika_core.wake_activation import WakeActivationEvidence, WakeActivationOutcome

_REQUEST_ID = "voice-owner-1"
_AUDIO = b"\x01\x00" * 4_000
_AUDIO_SHA256 = hashlib.sha256(_AUDIO).hexdigest()
_TRANSCRIPT = "Ніка, відкрий мої завдання"
_TRANSCRIPT_SHA256 = hashlib.sha256(_TRANSCRIPT.encode("utf-8")).hexdigest()
_PROFILE_ID_SHA256 = hashlib.sha256(b"oleksii-profile").hexdigest()
_PROFILE_REVISION_SHA256 = hashlib.sha256(b"profile-revision-v1").hexdigest()


def _voice(
    *,
    wake_outcome: WakeActivationOutcome = WakeActivationOutcome.DETECTED,
) -> VoiceTurnEvidence:
    detected = wake_outcome is WakeActivationOutcome.DETECTED
    return VoiceTurnEvidence(
        request_id=_REQUEST_ID,
        status=VoiceTurnStatus.COMPLETED,
        capture=MicrophoneCaptureEvidence(
            request_id=_REQUEST_ID,
            provider_id="sounddevice-wasapi",
            device_id_sha256="d" * 64,
            status=MicrophoneCaptureStatus.SUCCEEDED,
            sample_rate_hz=16_000,
            sample_count=4_000,
            audio_byte_count=len(_AUDIO),
            audio_sha256=_AUDIO_SHA256,
            latency_ms=250.0,
        ),
        transcription=SpeechToTextEvidence(
            request_id=_REQUEST_ID,
            provider_id="sherpa-onnx-whisper",
            model="whisper-uk-v1",
            status=SpeechToTextStatus.SUCCEEDED,
            privacy=PrivacyClass.SENSITIVE,
            audio_format=SpeechAudioFormat.PCM_S16LE,
            audio_sha256=_AUDIO_SHA256,
            audio_bytes=len(_AUDIO),
            sample_rate_hz=16_000,
            channels=1,
            requested_language="uk",
            detected_language=None,
            transcript_chars=len(_TRANSCRIPT),
            transcript_sha256=_TRANSCRIPT_SHA256,
            latency_ms=80.0,
        ),
        wake=WakeActivationEvidence(
            request_id=_REQUEST_ID,
            outcome=wake_outcome,
            transcript_sha256=_TRANSCRIPT_SHA256,
            token_count=4,
            matched_phrase_sha256="a" * 64 if detected else None,
            match_start_token=0 if detected else None,
            match_end_token_exclusive=1 if detected else None,
        ),
        activated=detected,
    )


def _speaker(
    outcome: SpeakerVerificationOutcome,
    confidence: float,
) -> SpeakerVerificationEvidence:
    return SpeakerVerificationEvidence(
        request_id=_REQUEST_ID,
        provider_id="local-speaker-engine",
        model_id="speaker-model-v1",
        profile_id_sha256=_PROFILE_ID_SHA256,
        profile_revision_sha256=_PROFILE_REVISION_SHA256,
        audio_sha256=_AUDIO_SHA256,
        audio_byte_count=len(_AUDIO),
        sample_rate_hz=16_000,
        duration_seconds=0.25,
        confidence=confidence,
        outcome=outcome,
    )


def test_wake_plus_match_produces_owner_authorized_evidence_only() -> None:
    evidence = OwnerVoiceAuthorizationGate().evaluate(
        voice_turn=_voice(),
        speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.93),
    )

    assert evidence.decision is VoiceAuthorizationDecision.OWNER_AUTHORIZED
    assert evidence.reason is VoiceAuthorizationReason.OWNER_MATCH
    assert evidence.capture_audio_sha256 == _AUDIO_SHA256
    assert evidence.transcript_sha256 == _TRANSCRIPT_SHA256
    assert evidence.speaker_profile_id_sha256 == _PROFILE_ID_SHA256
    assert evidence.speaker_profile_revision_sha256 == _PROFILE_REVISION_SHA256
    assert evidence.speaker_confidence == 0.93
    assert not hasattr(evidence, "execute")
    assert not hasattr(evidence, "approved")


def test_uncertain_speaker_requires_confirmation_when_wake_detected() -> None:
    evidence = OwnerVoiceAuthorizationGate().evaluate(
        voice_turn=_voice(),
        speaker=_speaker(SpeakerVerificationOutcome.UNCERTAIN, 0.72),
    )

    assert evidence.decision is VoiceAuthorizationDecision.CONFIRMATION_REQUIRED
    assert evidence.reason is VoiceAuthorizationReason.SPEAKER_UNCERTAIN


def test_no_match_is_denied_even_when_wake_detected() -> None:
    evidence = OwnerVoiceAuthorizationGate().evaluate(
        voice_turn=_voice(),
        speaker=_speaker(SpeakerVerificationOutcome.NO_MATCH, 0.20),
    )

    assert evidence.decision is VoiceAuthorizationDecision.DENIED
    assert evidence.reason is VoiceAuthorizationReason.SPEAKER_NO_MATCH


@pytest.mark.parametrize(
    ("speaker_outcome", "confidence"),
    [
        (SpeakerVerificationOutcome.MATCH, 0.95),
        (SpeakerVerificationOutcome.UNCERTAIN, 0.72),
        (SpeakerVerificationOutcome.NO_MATCH, 0.20),
    ],
)
def test_missing_wake_always_denies(
    speaker_outcome: SpeakerVerificationOutcome,
    confidence: float,
) -> None:
    evidence = OwnerVoiceAuthorizationGate().evaluate(
        voice_turn=_voice(wake_outcome=WakeActivationOutcome.NOT_DETECTED),
        speaker=_speaker(speaker_outcome, confidence),
    )

    assert evidence.decision is VoiceAuthorizationDecision.DENIED
    assert evidence.reason is VoiceAuthorizationReason.WAKE_NOT_DETECTED


@pytest.mark.parametrize(
    "speaker",
    [
        dataclasses.replace(
            _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
            request_id="different-request",
        ),
        dataclasses.replace(
            _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
            audio_sha256="0" * 64,
        ),
        dataclasses.replace(
            _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
            audio_byte_count=len(_AUDIO) + 2,
            duration_seconds=(len(_AUDIO) + 2) / (16_000 * 2),
        ),
        dataclasses.replace(
            _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
            sample_rate_hz=8_000,
            duration_seconds=len(_AUDIO) / (8_000 * 2),
        ),
    ],
)
def test_speaker_evidence_must_bind_the_same_voice_turn(
    speaker: SpeakerVerificationEvidence,
) -> None:
    with pytest.raises(VoiceAuthorizationError):
        OwnerVoiceAuthorizationGate().evaluate(voice_turn=_voice(), speaker=speaker)


def test_transcription_must_bind_capture_audio() -> None:
    voice = _voice()
    assert voice.transcription is not None
    mutated = dataclasses.replace(
        voice,
        transcription=dataclasses.replace(voice.transcription, audio_sha256="0" * 64),
    )

    with pytest.raises(VoiceAuthorizationError, match="captured audio"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=mutated,
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_wake_must_bind_transcription() -> None:
    voice = _voice()
    assert voice.wake is not None
    mutated = dataclasses.replace(
        voice,
        wake=dataclasses.replace(voice.wake, transcript_sha256="0" * 64),
    )

    with pytest.raises(VoiceAuthorizationError, match="transcription"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=mutated,
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_voice_activation_flag_cannot_disagree_with_wake() -> None:
    with pytest.raises(VoiceAuthorizationError, match="activated flag"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=dataclasses.replace(_voice(), activated=False),
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.01, 1.01, True])
def test_malformed_speaker_confidence_fails_closed(confidence: float) -> None:
    speaker = dataclasses.replace(
        _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        confidence=confidence,
    )

    with pytest.raises(VoiceAuthorizationError, match="confidence"):
        OwnerVoiceAuthorizationGate().evaluate(voice_turn=_voice(), speaker=speaker)


def test_incomplete_voice_turn_cannot_be_authorized() -> None:
    with pytest.raises(VoiceAuthorizationError, match="successfully completed"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=dataclasses.replace(
                _voice(),
                status=VoiceTurnStatus.TRANSCRIPTION_FAILED,
            ),
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_detected_wake_must_carry_valid_match_geometry() -> None:
    voice = _voice()
    assert voice.wake is not None
    bad_wake = dataclasses.replace(voice.wake, matched_phrase_sha256=None)

    with pytest.raises(VoiceAuthorizationError, match="sha256"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=dataclasses.replace(voice, wake=bad_wake),
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_non_detected_wake_cannot_carry_match_geometry() -> None:
    voice = _voice(wake_outcome=WakeActivationOutcome.NOT_DETECTED)
    assert voice.wake is not None
    bad_wake = dataclasses.replace(
        voice.wake,
        matched_phrase_sha256="a" * 64,
        match_start_token=0,
        match_end_token_exclusive=1,
    )

    with pytest.raises(VoiceAuthorizationError, match="must not carry"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=dataclasses.replace(voice, wake=bad_wake),
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_reportable_evidence_contains_hashes_not_raw_profile_or_transcript() -> None:
    evidence = OwnerVoiceAuthorizationGate().evaluate(
        voice_turn=_voice(),
        speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
    )
    rendered = repr(evidence.as_dict())

    assert "oleksii-profile" not in rendered
    assert _TRANSCRIPT not in rendered
    assert evidence.as_dict()["schema"] == "nika.voice-authorization-evidence:v1"
    assert evidence.as_dict()["decision"] == "owner_authorized"


def test_capture_pcm_geometry_is_revalidated() -> None:
    voice = _voice()
    mutated = dataclasses.replace(
        voice,
        capture=dataclasses.replace(
            voice.capture,
            audio_byte_count=voice.capture.audio_byte_count + 2,
        ),
    )

    with pytest.raises(VoiceAuthorizationError, match="PCM16"):
        OwnerVoiceAuthorizationGate().evaluate(
            voice_turn=mutated,
            speaker=_speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        )


def test_speaker_duration_geometry_is_revalidated() -> None:
    speaker = dataclasses.replace(
        _speaker(SpeakerVerificationOutcome.MATCH, 0.95),
        duration_seconds=0.5,
    )

    with pytest.raises(VoiceAuthorizationError, match="duration"):
        OwnerVoiceAuthorizationGate().evaluate(voice_turn=_voice(), speaker=speaker)
