from __future__ import annotations

import asyncio

from nika_core.microphone_capture import (
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureFailureCode,
    MicrophoneCapturePolicy,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
    MicrophoneCaptureService,
    MicrophoneCaptureStatus,
)


def _request(
    *,
    request_id: str = "capture-1",
    timeout: float = 0.005,
) -> MicrophoneCaptureRequest:
    return MicrophoneCaptureRequest(
        request_id=request_id,
        provider_id="local-microphone",
        device_id="default-input",
        sample_rate_hz=16_000,
        sample_count=1_600,
        policy=MicrophoneCapturePolicy(timeout_seconds=timeout),
    )


class _LateAfterCancellationAdapter:
    def __init__(self) -> None:
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id="local-microphone",
            device_id="default-input",
        )
        self.calls = 0
        self.cleanup_started = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cleanup_started.set()
                await asyncio.sleep(0.08)
                self.cleanup_finished.set()
                return MicrophoneCaptureResponse(
                    request_id=request.request_id,
                    provider_id=request.provider_id,
                    device_id=request.device_id,
                    sample_rate_hz=request.sample_rate_hz,
                    pcm_s16le=b"\x01\x00" * request.sample_count,
                )
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=b"\x02\x00" * request.sample_count,
        )


def test_late_success_after_cancel_cannot_cross_timeout_boundary() -> None:
    async def scenario() -> None:
        adapter = _LateAfterCancellationAdapter()
        service = MicrophoneCaptureService(adapter)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await service.capture(_request())
        elapsed = loop.time() - started

        assert elapsed < 0.05
        assert result.evidence.status is MicrophoneCaptureStatus.FAILED
        assert result.evidence.error_code is MicrophoneCaptureFailureCode.TIMEOUT
        assert result.evidence.cleanup_pending is True
        assert result.pcm_s16le is None
        assert service.cleanup_pending is True

        blocked = await service.capture(_request(request_id="capture-2"))
        assert blocked.evidence.error_code is MicrophoneCaptureFailureCode.CLEANUP_PENDING
        assert blocked.evidence.cleanup_pending is True
        assert adapter.calls == 1

        await asyncio.wait_for(adapter.cleanup_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert service.cleanup_pending is False

        resumed = await service.capture(_request(request_id="capture-3", timeout=0.2))
        assert resumed.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert resumed.pcm_s16le == b"\x02\x00" * 1_600
        assert adapter.calls == 2

    asyncio.run(scenario())


def test_caller_cancel_returns_without_waiting_for_slow_adapter_cleanup() -> None:
    async def scenario() -> None:
        adapter = _LateAfterCancellationAdapter()
        service = MicrophoneCaptureService(adapter)
        task = asyncio.create_task(service.capture(_request(timeout=1.0)))
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation must propagate")
        assert loop.time() - started < 0.05
        assert service.cleanup_pending is True
        await asyncio.wait_for(adapter.cleanup_finished.wait(), timeout=0.5)

    asyncio.run(scenario())
