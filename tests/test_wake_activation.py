from __future__ import annotations

import pytest

from nika_core.wake_activation import (
    MAX_TRANSCRIPT_CHARS,
    WakeActivationDetector,
    WakeActivationError,
    WakeActivationOutcome,
    WakeActivationPolicy,
)


def test_detects_ukrainian_wake_word_as_exact_token() -> None:
    detector = WakeActivationDetector()

    evidence = detector.detect(
        request_id="wake-1",
        transcript="НіКА, відкрий мої завдання.",
    )

    assert evidence.outcome is WakeActivationOutcome.DETECTED
    assert evidence.match_start_token == 0
    assert evidence.match_end_token_exclusive == 1
    assert evidence.matched_phrase_sha256 is not None


def test_detects_latin_alias_after_other_words() -> None:
    detector = WakeActivationDetector()

    evidence = detector.detect(
        request_id="wake-2",
        transcript="Please, NIKA, continue the task",
    )

    assert evidence.outcome is WakeActivationOutcome.DETECTED
    assert evidence.match_start_token == 1
    assert evidence.match_end_token_exclusive == 2


@pytest.mark.parametrize("transcript", ["механіка", "mechanika", "NikaCore", "без активації"])
def test_substrings_do_not_activate(transcript: str) -> None:
    detector = WakeActivationDetector()

    evidence = detector.detect(request_id="wake-safe", transcript=transcript)

    assert evidence.outcome is WakeActivationOutcome.NOT_DETECTED
    assert evidence.matched_phrase_sha256 is None
    assert evidence.match_start_token is None


def test_custom_multiword_phrase_matches_contiguous_tokens() -> None:
    detector = WakeActivationDetector(WakeActivationPolicy(("привіт ніка",)))

    detected = detector.detect(request_id="wake-3", transcript="Привіт, Ніка! Слухай.")
    not_detected = detector.detect(request_id="wake-4", transcript="Привіт усім, Ніка")

    assert detected.outcome is WakeActivationOutcome.DETECTED
    assert detected.match_end_token_exclusive - detected.match_start_token == 2
    assert not_detected.outcome is WakeActivationOutcome.NOT_DETECTED


def test_evidence_is_privacy_minimized_and_binds_exact_transcript() -> None:
    detector = WakeActivationDetector()
    transcript = "Ніка, секретний текст після активації"

    evidence = detector.detect(request_id="wake-private", transcript=transcript)

    assert evidence.outcome is WakeActivationOutcome.DETECTED
    assert len(evidence.transcript_sha256) == 64
    assert transcript not in repr(evidence)
    assert "секретний" not in repr(evidence)
    assert not hasattr(evidence, "transcript")


def test_empty_transcript_is_a_safe_non_activation() -> None:
    evidence = WakeActivationDetector().detect(request_id="wake-empty", transcript="")

    assert evidence.outcome is WakeActivationOutcome.NOT_DETECTED
    assert evidence.token_count == 0


@pytest.mark.parametrize(
    "phrases",
    [
        (),
        ["ніка"],
        ("ніка", "НІКА"),
        ("\x00ніка",),
        ("!!!",),
    ],
)
def test_invalid_or_ambiguous_policy_fails_closed(phrases: object) -> None:
    with pytest.raises(WakeActivationError):
        WakeActivationPolicy(phrases)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("request_id", "transcript"),
    [
        ("bad request", "Ніка"),
        ("wake-control", "Ніка\x00 далі"),
        ("wake-long", "a" * (MAX_TRANSCRIPT_CHARS + 1)),
    ],
)
def test_invalid_request_material_fails_closed(request_id: str, transcript: str) -> None:
    with pytest.raises(WakeActivationError):
        WakeActivationDetector().detect(request_id=request_id, transcript=transcript)


def test_detection_is_repeatable_for_identical_input() -> None:
    detector = WakeActivationDetector()

    first = detector.detect(request_id="wake-repeat", transcript="Гей, Ніка!")
    second = detector.detect(request_id="wake-repeat", transcript="Гей, Ніка!")

    assert first == second
