from __future__ import annotations

import math
import struct

import pytest

from nika_core.voice_activity import VoiceActivityConfig, VoiceActivityDetector


def _frame(amplitude: int, samples: int = 320) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def test_silence_stays_inactive_and_exposes_bounded_numeric_evidence() -> None:
    detector = VoiceActivityDetector()

    decision = detector.process(_frame(0))

    assert decision.speech_active is False
    assert decision.started is False
    assert decision.ended is False
    assert decision.rms == 0.0
    assert decision.sample_count == 320


def test_attack_requires_consecutive_frames_before_start() -> None:
    detector = VoiceActivityDetector(
        VoiceActivityConfig(attack_frames=2, release_frames=2)
    )

    first = detector.process(_frame(2_000))
    second = detector.process(_frame(2_000))

    assert first.speech_active is False
    assert first.started is False
    assert second.speech_active is True
    assert second.started is True
    assert second.ended is False


def test_release_requires_consecutive_quiet_frames_before_end() -> None:
    detector = VoiceActivityDetector(
        VoiceActivityConfig(attack_frames=1, release_frames=2)
    )
    detector.process(_frame(2_000))

    first_quiet = detector.process(_frame(0))
    second_quiet = detector.process(_frame(0))

    assert first_quiet.speech_active is True
    assert first_quiet.ended is False
    assert second_quiet.speech_active is False
    assert second_quiet.ended is True


def test_hysteresis_does_not_chatter_between_start_and_stop_thresholds() -> None:
    config = VoiceActivityConfig(
        start_rms=0.05,
        stop_rms=0.02,
        attack_frames=1,
        release_frames=2,
    )
    detector = VoiceActivityDetector(config)
    middle = _frame(1_100)

    before_start = detector.process(middle)
    started = detector.process(_frame(2_000))
    held = detector.process(middle)

    assert 0.02 < held.rms < 0.05
    assert before_start.speech_active is False
    assert started.started is True
    assert held.speech_active is True
    assert held.ended is False


def test_reset_clears_only_detector_state() -> None:
    detector = VoiceActivityDetector(VoiceActivityConfig(attack_frames=1))
    assert detector.process(_frame(2_000)).speech_active is True

    detector.reset()

    assert detector.speech_active is False
    decision = detector.process(_frame(0))
    assert decision.speech_active is False
    assert decision.started is False


def test_memoryview_input_is_consumed_without_retaining_mutable_audio() -> None:
    audio = bytearray(_frame(2_000))
    detector = VoiceActivityDetector(VoiceActivityConfig(attack_frames=1))

    decision = detector.process(memoryview(audio))
    audio[:] = b"\x00" * len(audio)

    assert decision.speech_active is True
    assert decision.rms > 0.05


@pytest.mark.parametrize("value", [b"", b"\x00", "audio", object()])
def test_invalid_pcm_input_fails_closed(value) -> None:
    detector = VoiceActivityDetector()

    with pytest.raises((TypeError, ValueError)):
        detector.process(value)


def test_frame_size_is_bounded_by_sample_rate_and_duration() -> None:
    detector = VoiceActivityDetector(
        VoiceActivityConfig(sample_rate_hz=16_000, max_frame_ms=20)
    )

    detector.process(_frame(0, samples=320))
    with pytest.raises(ValueError, match="frame bound"):
        detector.process(_frame(0, samples=321))


def test_rms_uses_signed_pcm16_full_scale() -> None:
    detector = VoiceActivityDetector(VoiceActivityConfig(attack_frames=1))

    negative = detector.process(_frame(-32_768, samples=1))

    assert math.isclose(negative.rms, 1.0)
    assert negative.sample_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate_hz": True},
        {"sample_rate_hz": 7_999},
        {"start_rms": float("nan")},
        {"start_rms": 1.1},
        {"stop_rms": 0.2, "start_rms": 0.1},
        {"attack_frames": 0},
        {"release_frames": False},
        {"max_frame_ms": 0},
    ],
)
def test_invalid_configuration_fails_closed(kwargs) -> None:
    with pytest.raises(ValueError):
        VoiceActivityConfig(**kwargs)


def test_config_object_type_is_required() -> None:
    with pytest.raises(TypeError, match="VoiceActivityConfig"):
        VoiceActivityDetector(config=object())
