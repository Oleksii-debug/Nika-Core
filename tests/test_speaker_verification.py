from __future__ import annotations

import math
import threading

import pytest

from nika_core.speaker_verification import (
    MAX_AUDIO_SECONDS,
    SpeakerVerificationError,
    SpeakerVerificationErrorCode,
    SpeakerVerificationOutcome,
    SpeakerVerificationPolicy,
    SpeakerVerificationRequest,
    SpeakerVerificationService,
    SpeakerVerifierCapabilities,
    SpeakerVerifierResponse,
    UnavailableSpeakerVerifierAdapter,
)


def _pcm(seconds: float = 1.0, sample_rate_hz: int = 16_000) -> bytes:
    return b"\x00\x00" * int(seconds * sample_rate_hz)


class FakeVerifier:
    def __init__(
        self,
        *,
        confidence: float = 0.90,
        capabilities: SpeakerVerifierCapabilities | None = None,
    ) -> None:
        self._capabilities = capabilities or SpeakerVerifierCapabilities(
            provider_id="local-speaker-engine",
            model_id="speaker-model-v1",
        )
        self.confidence = confidence
        self.calls = 0
        self.response_provider_id: str | None = None
        self.response_model_id: str | None = None
        self.response_profile_id: str | None = None
        self.failure: BaseException | None = None
        self.seen_timeout: float | None = None

    @property
    def capabilities(self) -> SpeakerVerifierCapabilities:
        return self._capabilities

    @capabilities.setter
    def capabilities(self, value: SpeakerVerifierCapabilities) -> None:
        self._capabilities = value

    def verify(
        self,
        request: SpeakerVerificationRequest,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> SpeakerVerifierResponse:
        self.calls += 1
        self.seen_timeout = timeout_seconds
        assert cancel_event is None or not cancel_event.is_set()
        if self.failure is not None:
            raise self.failure
        return SpeakerVerifierResponse(
            provider_id=self.response_provider_id or self.capabilities.provider_id,
            model_id=self.response_model_id or self.capabilities.model_id,
            profile_id=self.response_profile_id or request.profile_id,
            confidence=self.confidence,
        )


def _request(*, profile_id: str = "owner-profile") -> SpeakerVerificationRequest:
    return SpeakerVerificationRequest(
        request_id="voice-req-1",
        profile_id=profile_id,
        pcm_s16le=_pcm(),
    )


def test_match_returns_privacy_minimized_route_bound_evidence() -> None:
    adapter = FakeVerifier(confidence=0.91)
    service = SpeakerVerificationService(adapter)
    request = _request(profile_id="owner-profile")

    evidence = service.verify(request, timeout_seconds=4)

    assert evidence.outcome is SpeakerVerificationOutcome.MATCH
    assert evidence.confidence == 0.91
    assert evidence.provider_id == "local-speaker-engine"
    assert evidence.model_id == "speaker-model-v1"
    assert evidence.audio_byte_count == len(request.pcm_s16le)
    assert len(evidence.audio_sha256) == 64
    assert len(evidence.profile_fingerprint_sha256) == 64
    assert "owner-profile" not in repr(evidence)
    assert not hasattr(evidence, "pcm_s16le")
    assert not hasattr(evidence, "profile_id")
    assert adapter.seen_timeout == 4.0


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.60, SpeakerVerificationOutcome.NO_MATCH),
        (0.61, SpeakerVerificationOutcome.UNCERTAIN),
        (0.84, SpeakerVerificationOutcome.UNCERTAIN),
        (0.85, SpeakerVerificationOutcome.MATCH),
    ],
)
def test_confidence_policy_is_deterministic(
    confidence: float,
    expected: SpeakerVerificationOutcome,
) -> None:
    service = SpeakerVerificationService(FakeVerifier(confidence=confidence))

    assert service.verify(_request()).outcome is expected


def test_custom_policy_requires_strict_threshold_order() -> None:
    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationPolicy(no_match_at_or_below=0.9, match_at_or_above=0.8)

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST


def test_audio_bounds_are_enforced_before_adapter_effect() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationRequest(
            request_id="too-long",
            profile_id="owner-profile",
            pcm_s16le=_pcm(MAX_AUDIO_SECONDS + 0.1),
        )

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0
    assert service.capabilities.provider_id == "local-speaker-engine"


def test_cancelled_request_is_effect_free() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request(), cancel_event=cancelled)

    assert error.value.code is SpeakerVerificationErrorCode.CANCELLED
    assert adapter.calls == 0


def test_adapter_route_change_before_effect_fails_closed() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)
    adapter.capabilities = SpeakerVerifierCapabilities(
        provider_id="different-engine",
        model_id="speaker-model-v1",
    )

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ROUTE_MISMATCH
    assert adapter.calls == 0


@pytest.mark.parametrize("field", ["provider", "model"])
def test_response_route_mismatch_never_becomes_match(field: str) -> None:
    adapter = FakeVerifier(confidence=0.99)
    if field == "provider":
        adapter.response_provider_id = "other-provider"
    else:
        adapter.response_model_id = "other-model"
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ROUTE_MISMATCH


def test_response_profile_mismatch_fails_closed() -> None:
    adapter = FakeVerifier(confidence=0.99)
    adapter.response_profile_id = "someone-else"
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("confidence", [math.nan, math.inf, -0.1, 1.1, True])
def test_invalid_adapter_confidence_cannot_become_evidence(confidence: object) -> None:
    adapter = FakeVerifier()
    adapter.confidence = confidence  # type: ignore[assignment]
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_RESPONSE


def test_raw_string_kind_is_rejected_even_for_local_value() -> None:
    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerifierCapabilities(
            provider_id="local-speaker-engine",
            model_id="speaker-model-v1",
            kind="local",  # type: ignore[arg-type]
        )

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST


def test_unavailable_adapter_is_explicit_and_does_not_fabricate_identity() -> None:
    service = SpeakerVerificationService(UnavailableSpeakerVerifierAdapter())

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_UNAVAILABLE
    assert error.value.retryable is True


def test_unknown_adapter_error_is_minimized() -> None:
    adapter = FakeVerifier()
    adapter.failure = RuntimeError("SENSITIVE_DIAGNOSTIC_CANARY")
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert "SENSITIVE_DIAGNOSTIC" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, 301, True, math.inf, "5"])
def test_timeout_is_bounded_and_typed(timeout: object) -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request(), timeout_seconds=timeout)  # type: ignore[arg-type]

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0
