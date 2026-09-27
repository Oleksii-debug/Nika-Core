from __future__ import annotations

import hashlib
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

_PROFILE_REVISION_SHA256 = "a" * 64


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
        self.response_profile_revision_sha256: str | None = None
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
        profile_revision_sha256 = (
            self.response_profile_revision_sha256
            if self.response_profile_revision_sha256 is not None
            else request.profile_revision_sha256
        )
        return SpeakerVerifierResponse(
            provider_id=self.response_provider_id or self.capabilities.provider_id,
            model_id=self.response_model_id or self.capabilities.model_id,
            profile_id=self.response_profile_id or request.profile_id,
            profile_revision_sha256=profile_revision_sha256,
            confidence=self.confidence,
        )


class _CapabilitiesFailingVerifier(FakeVerifier):
    def __init__(self, *, fail_on_read: int) -> None:
        super().__init__()
        self._capability_reads = 0
        self._fail_on_read = fail_on_read

    @property
    def capabilities(self) -> SpeakerVerifierCapabilities:
        self._capability_reads += 1
        if self._capability_reads == self._fail_on_read:
            raise RuntimeError("SENSITIVE_CAPABILITY_DIAGNOSTIC_CANARY")
        return self._capabilities


class _RequestMutatingVerifier(FakeVerifier):
    def verify(
        self,
        request: SpeakerVerificationRequest,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> SpeakerVerifierResponse:
        original_profile = request.profile_id
        original_revision = request.profile_revision_sha256
        object.__setattr__(request, "request_id", "adapter-mutated-request")
        object.__setattr__(request, "pcm_s16le", b"\x01\x00" * 16_000)
        self.calls += 1
        self.seen_timeout = timeout_seconds
        return SpeakerVerifierResponse(
            provider_id=self._capabilities.provider_id,
            model_id=self._capabilities.model_id,
            profile_id=original_profile,
            profile_revision_sha256=original_revision,
            confidence=self.confidence,
        )


class _BehavioralStr(str):
    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


class _BehavioralSpeakerError(SpeakerVerificationError):
    def __getattribute__(self, name: str) -> object:
        if name in {"code", "retryable"}:
            raise RuntimeError("SENSITIVE_ERROR_ATTRIBUTE_CANARY")
        return super().__getattribute__(name)


def _request(
    *,
    profile_id: str = "owner-profile",
    profile_revision_sha256: str = _PROFILE_REVISION_SHA256,
) -> SpeakerVerificationRequest:
    return SpeakerVerificationRequest(
        request_id="voice-req-1",
        profile_id=profile_id,
        profile_revision_sha256=profile_revision_sha256,
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
    assert len(evidence.profile_id_sha256) == 64
    assert evidence.profile_revision_sha256 == _PROFILE_REVISION_SHA256
    assert "owner-profile" not in repr(evidence)
    assert not hasattr(evidence, "pcm_s16le")
    assert not hasattr(evidence, "profile_id")
    assert not hasattr(evidence, "profile_fingerprint_sha256")
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


def test_noncanonical_policy_is_rejected_before_adapter_effect() -> None:
    adapter = FakeVerifier()

    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationService(adapter, policy=object())  # type: ignore[arg-type]

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0


def test_invalid_profile_revision_is_rejected_before_adapter_effect() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        _request(profile_revision_sha256="NOT-A-LOWERCASE-SHA256")

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0
    assert service.capabilities.provider_id == "local-speaker-engine"


def test_audio_bounds_are_enforced_before_adapter_effect() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationRequest(
            request_id="too-long",
            profile_id="owner-profile",
            profile_revision_sha256=_PROFILE_REVISION_SHA256,
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


def test_response_profile_revision_mismatch_fails_closed() -> None:
    adapter = FakeVerifier(confidence=0.99)
    adapter.response_profile_revision_sha256 = "b" * 64
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request(profile_id="owner-profile"))

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_RESPONSE


def test_invalid_response_profile_revision_format_fails_closed() -> None:
    adapter = FakeVerifier(confidence=0.99)
    adapter.response_profile_revision_sha256 = "A" * 64
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


def test_capability_read_failure_at_construction_is_minimized() -> None:
    adapter = _CapabilitiesFailingVerifier(fail_on_read=1)

    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationService(adapter)

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert "SENSITIVE_CAPABILITY_DIAGNOSTIC" not in str(error.value)
    assert adapter.calls == 0


def test_capability_read_failure_before_effect_is_minimized() -> None:
    adapter = _CapabilitiesFailingVerifier(fail_on_read=2)
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert "SENSITIVE_CAPABILITY_DIAGNOSTIC" not in str(error.value)
    assert adapter.calls == 0


def test_capability_read_failure_after_effect_is_minimized() -> None:
    adapter = _CapabilitiesFailingVerifier(fail_on_read=4)
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert "SENSITIVE_CAPABILITY_DIAGNOSTIC" not in str(error.value)
    assert adapter.calls == 1


@pytest.mark.parametrize("timeout", [0, -1, 301, True, math.inf, "5"])
def test_timeout_is_bounded_and_typed(timeout: object) -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request(), timeout_seconds=timeout)  # type: ignore[arg-type]

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0


def test_float_overflowing_timeout_fails_before_adapter_effect() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request(), timeout_seconds=1 << 100_000)

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0


def test_float_overflowing_policy_threshold_fails_as_invalid_request() -> None:
    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationPolicy(no_match_at_or_below=1 << 100_000)

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST


def test_float_overflowing_response_confidence_fails_as_invalid_response() -> None:
    adapter = FakeVerifier()
    adapter.confidence = 1 << 100_000  # type: ignore[assignment]
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_RESPONSE


def test_adapter_capability_alias_mutation_is_detected_as_route_drift() -> None:
    adapter = FakeVerifier()
    capability_alias = adapter.capabilities
    service = SpeakerVerificationService(adapter)

    object.__setattr__(capability_alias, "provider_id", "different-engine")

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ROUTE_MISMATCH
    assert adapter.calls == 0


def test_public_capabilities_are_detached_from_internal_authority() -> None:
    service = SpeakerVerificationService(FakeVerifier())
    exposed = service.capabilities

    object.__setattr__(exposed, "provider_id", "different-engine")

    assert service.capabilities.provider_id == "local-speaker-engine"
    assert service.verify(_request()).provider_id == "local-speaker-engine"


def test_caller_policy_mutation_cannot_change_bound_classification() -> None:
    policy = SpeakerVerificationPolicy(no_match_at_or_below=0.60, match_at_or_above=0.85)
    service = SpeakerVerificationService(FakeVerifier(confidence=0.70), policy=policy)

    object.__setattr__(policy, "match_at_or_above", 0.65)

    assert service.verify(_request()).outcome is SpeakerVerificationOutcome.UNCERTAIN


def test_adapter_request_mutation_cannot_rewrite_retained_evidence_input() -> None:
    request = _request()
    expected_audio_sha256 = hashlib.sha256(request.pcm_s16le).hexdigest()
    service = SpeakerVerificationService(_RequestMutatingVerifier())

    evidence = service.verify(request)

    assert evidence.request_id == "voice-req-1"
    assert evidence.audio_sha256 == expected_audio_sha256
    assert request.request_id == "voice-req-1"


def test_forged_request_is_revalidated_before_adapter_effect() -> None:
    adapter = FakeVerifier()
    service = SpeakerVerificationService(adapter)
    request = _request()
    object.__setattr__(request, "profile_id", "invalid profile id with spaces")

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(request)

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0


def test_behavioral_string_request_identity_is_rejected() -> None:
    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationRequest(
            request_id=_BehavioralStr("voice-req-1"),
            profile_id="owner-profile",
            profile_revision_sha256=_PROFILE_REVISION_SHA256,
            pcm_s16le=_pcm(),
        )

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST


def test_behavioral_response_route_cannot_spoof_bound_authority() -> None:
    response = SpeakerVerifierResponse(
        provider_id=_BehavioralStr("different-engine"),
        model_id="speaker-model-v1",
        profile_id="owner-profile",
        profile_revision_sha256=_PROFILE_REVISION_SHA256,
        confidence=0.99,
    )

    class _BehavioralResponseVerifier(FakeVerifier):
        def verify(
            self,
            request: SpeakerVerificationRequest,
            *,
            timeout_seconds: float,
            cancel_event: threading.Event | None,
        ) -> SpeakerVerifierResponse:
            del request, timeout_seconds, cancel_event
            self.calls += 1
            return response

    service = SpeakerVerificationService(_BehavioralResponseVerifier())
    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_RESPONSE


def test_adapter_typed_error_diagnostic_is_minimized() -> None:
    adapter = FakeVerifier()
    adapter.failure = SpeakerVerificationError(
        SpeakerVerificationErrorCode.ADAPTER_FAILURE,
        "SENSITIVE_TYPED_ADAPTER_DIAGNOSTIC",
        retryable=True,
    )
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert error.value.retryable is True
    assert "SENSITIVE_TYPED_ADAPTER_DIAGNOSTIC" not in str(error.value)


def test_forged_policy_is_revalidated_at_service_boundary() -> None:
    adapter = FakeVerifier()
    policy = SpeakerVerificationPolicy()
    object.__setattr__(policy, "match_at_or_above", math.nan)

    with pytest.raises(SpeakerVerificationError) as error:
        SpeakerVerificationService(adapter, policy=policy)

    assert error.value.code is SpeakerVerificationErrorCode.INVALID_REQUEST
    assert adapter.calls == 0



def test_behavioral_adapter_error_subclass_cannot_escape_sanitizer() -> None:
    adapter = FakeVerifier()
    adapter.failure = _BehavioralSpeakerError(
        SpeakerVerificationErrorCode.ADAPTER_FAILURE,
        "SENSITIVE_ERROR_MESSAGE_CANARY",
    )
    service = SpeakerVerificationService(adapter)

    with pytest.raises(SpeakerVerificationError) as error:
        service.verify(_request())

    assert type(error.value) is SpeakerVerificationError
    assert error.value.code is SpeakerVerificationErrorCode.ADAPTER_FAILURE
    assert "SENSITIVE" not in str(error.value)
