from __future__ import annotations

import asyncio
import hashlib

import pytest

from nika_core.speaker_diarization import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationPolicy,
    DiarizationRequest,
    DiarizerCapabilities,
    DiarizerResponse,
    DiarizerSegment,
    SpeakerDiarizationService,
    UnavailableSpeakerDiarizerAdapter,
)


def _audio(*, sample_rate_hz: int = 16_000, seconds: float = 1.0) -> bytes:
    sample_count = int(sample_rate_hz * seconds)
    return b"\x00\x00" * sample_count


def _request(*, seconds: float = 1.0) -> DiarizationRequest:
    return DiarizationRequest(
        request_id="req-1",
        pcm_s16le=_audio(seconds=seconds),
        sample_rate_hz=16_000,
    )


class _Adapter:
    def __init__(
        self,
        *,
        supports_overlap: bool = False,
        max_speakers: int = 8,
        segments: tuple[DiarizerSegment, ...] | None = None,
        latency_ms: float | None = 12.5,
    ) -> None:
        self.current_capabilities = DiarizerCapabilities(
            provider_id="local-diarizer",
            model_id="diarizer-v1",
            supports_overlap=supports_overlap,
            max_speakers=max_speakers,
        )
        self.segments = (
            segments
            if segments is not None
            else (
                DiarizerSegment(0, 400, "raw-speaker-A", 0.91),
                DiarizerSegment(400, 900, "raw-speaker-B", 0.82),
            )
        )
        self.latency_ms = latency_ms
        self.calls = 0

    @property
    def capabilities(self) -> DiarizerCapabilities:
        return self.current_capabilities

    async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
        self.calls += 1
        return DiarizerResponse(
            request_id=request.request_id,
            provider_id=self.current_capabilities.provider_id,
            model_id=self.current_capabilities.model_id,
            source_audio_sha256=request.audio_sha256,
            segments=self.segments,
            latency_ms=self.latency_ms,
        )


def test_success_pseudonymizes_raw_labels_and_binds_audio() -> None:
    adapter = _Adapter()
    request = _request()

    result = asyncio.run(SpeakerDiarizationService(adapter).diarize(request))

    assert result.segments[0].speaker_index == 1
    assert result.segments[1].speaker_index == 2
    assert result.evidence.speaker_count == 2
    assert result.evidence.segment_count == 2
    assert result.evidence.audio_sha256 == hashlib.sha256(request.pcm_s16le).hexdigest()
    assert result.evidence.overlap_detected is False
    rendered = repr(result.evidence.as_dict())
    assert "raw-speaker-A" not in rendered
    assert "raw-speaker-B" not in rendered
    assert len(result.evidence.timeline_sha256) == 64


def test_empty_valid_timeline_is_truthful_no_speech_result() -> None:
    result = asyncio.run(
        SpeakerDiarizationService(_Adapter(segments=())).diarize(_request())
    )

    assert result.segments == ()
    assert result.evidence.segment_count == 0
    assert result.evidence.speaker_count == 0


def test_same_structural_timeline_has_same_privacy_safe_digest() -> None:
    first = _Adapter(
        segments=(
            DiarizerSegment(0, 300, "engine-alpha", 0.9),
            DiarizerSegment(300, 700, "engine-beta", 0.8),
            DiarizerSegment(700, 900, "engine-alpha", 0.7),
        )
    )
    second = _Adapter(
        segments=(
            DiarizerSegment(0, 300, "different-one", 0.9),
            DiarizerSegment(300, 700, "different-two", 0.8),
            DiarizerSegment(700, 900, "different-one", 0.7),
        )
    )

    first_result = asyncio.run(SpeakerDiarizationService(first).diarize(_request()))
    second_result = asyncio.run(SpeakerDiarizationService(second).diarize(_request()))

    assert first_result.segments == second_result.segments
    assert first_result.evidence.timeline_sha256 == second_result.evidence.timeline_sha256


@pytest.mark.parametrize("field", ["request_id", "provider_id", "model_id"])
def test_response_identity_mismatch_fails_closed(field: str) -> None:
    class WrongIdentityAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            self.calls += 1
            values = {
                "request_id": request.request_id,
                "provider_id": self.current_capabilities.provider_id,
                "model_id": self.current_capabilities.model_id,
            }
            values[field] = "wrong"
            return DiarizerResponse(
                request_id=values["request_id"],
                provider_id=values["provider_id"],
                model_id=values["model_id"],
                source_audio_sha256=request.audio_sha256,
                segments=(),
            )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(WrongIdentityAdapter()).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ROUTE_MISMATCH


def test_source_audio_substitution_fails_closed() -> None:
    class WrongAudioAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            return DiarizerResponse(
                request_id=request.request_id,
                provider_id=self.current_capabilities.provider_id,
                model_id=self.current_capabilities.model_id,
                source_audio_sha256="0" * 64,
                segments=(),
            )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(WrongAudioAdapter()).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.INVALID_RESPONSE


def test_route_change_before_effect_prevents_adapter_call() -> None:
    adapter = _Adapter()
    service = SpeakerDiarizationService(adapter)
    adapter.current_capabilities = DiarizerCapabilities(
        provider_id="different-route",
        model_id="diarizer-v1",
        supports_overlap=False,
        max_speakers=8,
    )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(service.diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ROUTE_MISMATCH
    assert adapter.calls == 0


def test_route_change_during_effect_is_rejected() -> None:
    class MutatingAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            self.calls += 1
            original = self.current_capabilities
            response = DiarizerResponse(
                request_id=request.request_id,
                provider_id=original.provider_id,
                model_id=original.model_id,
                source_audio_sha256=request.audio_sha256,
                segments=(),
            )
            self.current_capabilities = DiarizerCapabilities(
                provider_id="replacement-route",
                model_id=original.model_id,
                supports_overlap=False,
                max_speakers=8,
            )
            return response

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(MutatingAdapter()).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ROUTE_MISMATCH


def test_capability_read_failure_is_minimized() -> None:
    class BrokenCapabilities:
        @property
        def capabilities(self) -> object:
            raise RuntimeError("CAPABILITY_SECRET")

    with pytest.raises(DiarizationError) as captured:
        SpeakerDiarizationService(BrokenCapabilities())  # type: ignore[arg-type]

    assert captured.value.code is DiarizationErrorCode.INVALID_REQUEST
    assert "CAPABILITY_SECRET" not in str(captured.value)


def test_default_policy_composes_with_smaller_adapter_capacity() -> None:
    adapter = _Adapter(
        max_speakers=1,
        segments=(
            DiarizerSegment(0, 400, "one", 0.8),
            DiarizerSegment(400, 900, "one", 0.9),
        ),
    )

    result = asyncio.run(SpeakerDiarizationService(adapter).diarize(_request()))

    assert result.evidence.speaker_count == 1


def test_adapter_capacity_remains_hard_speaker_ceiling() -> None:
    adapter = _Adapter(
        max_speakers=1,
        segments=(
            DiarizerSegment(0, 400, "one", 0.8),
            DiarizerSegment(400, 900, "two", 0.9),
        ),
    )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.RESOURCE_LIMIT


def test_policy_audio_budget_blocks_before_adapter_effect() -> None:
    adapter = _Adapter()
    request = _request(seconds=1.0)
    policy = DiarizationPolicy(max_audio_bytes=len(request.pcm_s16le) - 2)

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter, policy=policy).diarize(request))

    assert captured.value.code is DiarizationErrorCode.RESOURCE_LIMIT
    assert adapter.calls == 0


def test_policy_segment_ceiling_is_enforced() -> None:
    adapter = _Adapter(
        segments=(
            DiarizerSegment(0, 100, "one"),
            DiarizerSegment(100, 200, "one"),
        )
    )
    policy = DiarizationPolicy(max_segments=1)

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter, policy=policy).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.RESOURCE_LIMIT


@pytest.mark.parametrize(
    "segment",
    [
        DiarizerSegment(-1, 100, "speaker"),
        DiarizerSegment(100, 100, "speaker"),
        DiarizerSegment(100, 1_001, "speaker"),
        DiarizerSegment(0.0, 100, "speaker"),  # type: ignore[arg-type]
        DiarizerSegment(0, 100, "unsafe\nspeaker"),
        DiarizerSegment(0, 100, "speaker", float("nan")),
        DiarizerSegment(0, 100, "speaker", float("inf")),
        DiarizerSegment(0, 100, "speaker", 10**10_000),
    ],
)
def test_malformed_segments_fail_with_owned_invalid_response(
    segment: DiarizerSegment,
) -> None:
    adapter = _Adapter(segments=(segment,))

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.INVALID_RESPONSE


def test_unsorted_timeline_fails_closed() -> None:
    adapter = _Adapter(
        segments=(
            DiarizerSegment(500, 700, "one"),
            DiarizerSegment(100, 300, "one"),
        )
    )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.INVALID_RESPONSE


def test_overlap_requires_declared_adapter_capability() -> None:
    segments = (
        DiarizerSegment(0, 600, "one"),
        DiarizerSegment(400, 800, "two"),
    )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(
            SpeakerDiarizationService(
                _Adapter(supports_overlap=False, segments=segments)
            ).diarize(_request())
        )

    assert captured.value.code is DiarizationErrorCode.INVALID_RESPONSE


def test_declared_overlap_is_preserved_as_evidence() -> None:
    segments = (
        DiarizerSegment(0, 600, "one"),
        DiarizerSegment(400, 800, "two"),
    )

    result = asyncio.run(
        SpeakerDiarizationService(
            _Adapter(supports_overlap=True, segments=segments)
        ).diarize(_request())
    )

    assert result.evidence.overlap_detected is True
    assert result.evidence.speaker_count == 2


@pytest.mark.parametrize("latency", [float("nan"), float("inf"), -1.0, 10**10_000])
def test_invalid_latency_fails_closed(latency: object) -> None:
    adapter = _Adapter(latency_ms=latency)  # type: ignore[arg-type]

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(adapter).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, 10**10_000, True],
)
def test_invalid_timeout_policy_fails_without_float_overflow(timeout: object) -> None:
    with pytest.raises(DiarizationError) as captured:
        DiarizationPolicy(timeout_seconds=timeout)  # type: ignore[arg-type]

    assert captured.value.code is DiarizationErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("bad_policy", [False, 0, "", ()])
def test_falsy_non_policy_configuration_never_activates_defaults(
    bad_policy: object,
) -> None:
    with pytest.raises(DiarizationError) as captured:
        SpeakerDiarizationService(_Adapter(), policy=bad_policy)  # type: ignore[arg-type]

    assert captured.value.code is DiarizationErrorCode.INVALID_REQUEST


def test_unavailable_adapter_has_typed_minimized_failure() -> None:
    service = SpeakerDiarizationService(UnavailableSpeakerDiarizerAdapter())

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(service.diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ADAPTER_UNAVAILABLE
    assert captured.value.retryable is True
    assert "configured" not in str(captured.value)


def test_hostile_adapter_error_cannot_forge_service_taxonomy_or_leak_text() -> None:
    class HostileAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            del request
            raise DiarizationError(
                DiarizationErrorCode.INVALID_RESPONSE,
                "ADAPTER_SECRET_CANARY",
                retryable=True,
            )

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(HostileAdapter()).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ADAPTER_FAILURE
    assert captured.value.retryable is False
    assert "ADAPTER_SECRET_CANARY" not in str(captured.value)


def test_unknown_adapter_exception_is_minimized() -> None:
    class ExplodingAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            del request
            raise RuntimeError("PROVIDER_DIAGNOSTIC_SECRET")

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(ExplodingAdapter()).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ADAPTER_FAILURE
    assert "PROVIDER_DIAGNOSTIC_SECRET" not in str(captured.value)


def test_timeout_is_bounded_and_typed() -> None:
    class SlowAdapter(_Adapter):
        async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
            await asyncio.sleep(60)
            return await super().diarize(request)

    policy = DiarizationPolicy(timeout_seconds=0.01)

    with pytest.raises(DiarizationError) as captured:
        asyncio.run(SpeakerDiarizationService(SlowAdapter(), policy=policy).diarize(_request()))

    assert captured.value.code is DiarizationErrorCode.ADAPTER_TIMEOUT
    assert captured.value.retryable is True


def test_caller_cancellation_propagates() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class BlockingAdapter(_Adapter):
            async def diarize(self, request: DiarizationRequest) -> DiarizerResponse:
                entered.set()
                await asyncio.Event().wait()
                return await super().diarize(request)

        task = asyncio.create_task(
            SpeakerDiarizationService(BlockingAdapter()).diarize(_request())
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
