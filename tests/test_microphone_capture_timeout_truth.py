from __future__ import annotations

import asyncio
import threading

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
        self.capture_started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.calls += 1
        if self.calls == 1:
            self.capture_started.set()
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
        await asyncio.wait_for(adapter.capture_started.wait(), timeout=0.5)
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


class _BlockingCapabilitiesAdapter:
    def __init__(self, *, block_on_call: int) -> None:
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id="local-microphone",
            device_id="default-input",
        )
        self.block_on_call = block_on_call
        self.capability_calls = 0
        self.capture_calls = 0
        self.probe_started = threading.Event()
        self.release_probe = threading.Event()

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        self.capability_calls += 1
        if self.capability_calls == self.block_on_call:
            self.probe_started.set()
            self.release_probe.wait(timeout=2.0)
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.capture_calls += 1
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=b"\x04\x00" * request.sample_count,
        )


async def _wait_for_cleanup(service: MicrophoneCaptureService) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.5
    while service.cleanup_pending and loop.time() < deadline:
        await asyncio.sleep(0.005)
    assert service.cleanup_pending is False


def test_blocking_capability_discovery_times_out_before_capture_effect() -> None:
    async def scenario() -> None:
        adapter = _BlockingCapabilitiesAdapter(block_on_call=1)
        service = MicrophoneCaptureService(adapter)
        result = await service.capture(_request(timeout=0.02))

        assert adapter.probe_started.is_set()
        assert adapter.capture_calls == 0
        assert result.evidence.status is MicrophoneCaptureStatus.FAILED
        assert result.evidence.error_code is MicrophoneCaptureFailureCode.TIMEOUT
        assert result.evidence.cleanup_pending is True
        assert result.pcm_s16le is None
        assert service.cleanup_pending is True

        blocked = await service.capture(_request(request_id="capture-2", timeout=0.2))
        assert blocked.evidence.error_code is MicrophoneCaptureFailureCode.CLEANUP_PENDING
        assert adapter.capture_calls == 0

        adapter.release_probe.set()
        await _wait_for_cleanup(service)

        resumed = await service.capture(_request(request_id="capture-3", timeout=0.2))
        assert resumed.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert adapter.capture_calls == 1

    asyncio.run(scenario())


def test_blocking_post_capture_capability_probe_discards_pcm_on_timeout() -> None:
    async def scenario() -> None:
        adapter = _BlockingCapabilitiesAdapter(block_on_call=2)
        service = MicrophoneCaptureService(adapter)
        result = await service.capture(_request(timeout=0.02))

        assert adapter.probe_started.is_set()
        assert adapter.capture_calls == 1
        assert result.evidence.status is MicrophoneCaptureStatus.FAILED
        assert result.evidence.error_code is MicrophoneCaptureFailureCode.TIMEOUT
        assert result.evidence.cleanup_pending is True
        assert result.pcm_s16le is None

        adapter.release_probe.set()
        await _wait_for_cleanup(service)

        resumed = await service.capture(_request(request_id="capture-2", timeout=0.2))
        assert resumed.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert resumed.pcm_s16le == b"\x04\x00" * 1_600
        assert adapter.capture_calls == 2

    asyncio.run(scenario())


def test_caller_cancel_during_capability_discovery_propagates_and_fences_reentry() -> None:
    async def scenario() -> None:
        adapter = _BlockingCapabilitiesAdapter(block_on_call=1)
        service = MicrophoneCaptureService(adapter)
        task = asyncio.create_task(service.capture(_request(timeout=1.0)))

        loop = asyncio.get_running_loop()
        wait_deadline = loop.time() + 0.5
        while not adapter.probe_started.is_set() and loop.time() < wait_deadline:
            await asyncio.sleep(0.005)
        assert adapter.probe_started.is_set()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation must propagate")

        assert service.cleanup_pending is True
        blocked = await service.capture(_request(request_id="capture-2", timeout=0.2))
        assert blocked.evidence.error_code is MicrophoneCaptureFailureCode.CLEANUP_PENDING
        assert adapter.capture_calls == 0

        adapter.release_probe.set()
        await _wait_for_cleanup(service)

    asyncio.run(scenario())


class _HeldCaptureAdapter:
    def __init__(self) -> None:
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id="local-microphone",
            device_id="default-input",
        )
        self.capture_calls = 0
        self.active_captures = 0
        self.max_active_captures = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        self.capture_calls += 1
        call_number = self.capture_calls
        self.active_captures += 1
        self.max_active_captures = max(self.max_active_captures, self.active_captures)
        try:
            if call_number == 1:
                self.first_started.set()
                await self.release_first.wait()
            return MicrophoneCaptureResponse(
                request_id=request.request_id,
                provider_id=request.provider_id,
                device_id=request.device_id,
                sample_rate_hz=request.sample_rate_hz,
                pcm_s16le=b"\x05\x00" * request.sample_count,
            )
        finally:
            self.active_captures -= 1


def test_concurrent_short_waiter_times_out_without_second_microphone_effect() -> None:
    async def scenario() -> None:
        adapter = _HeldCaptureAdapter()
        service = MicrophoneCaptureService(adapter)
        first = asyncio.create_task(
            service.capture(_request(request_id="capture-first", timeout=0.5))
        )
        await asyncio.wait_for(adapter.first_started.wait(), timeout=0.2)

        second = await service.capture(_request(request_id="capture-second", timeout=0.02))

        assert second.evidence.error_code is MicrophoneCaptureFailureCode.TIMEOUT
        assert second.pcm_s16le is None
        assert adapter.capture_calls == 1
        assert adapter.max_active_captures == 1

        adapter.release_first.set()
        first_result = await asyncio.wait_for(first, timeout=0.2)
        assert first_result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert adapter.active_captures == 0

    asyncio.run(scenario())


def test_concurrent_long_waiter_runs_only_after_first_capture_releases() -> None:
    async def scenario() -> None:
        adapter = _HeldCaptureAdapter()
        service = MicrophoneCaptureService(adapter)
        first = asyncio.create_task(
            service.capture(_request(request_id="capture-first", timeout=0.5))
        )
        await asyncio.wait_for(adapter.first_started.wait(), timeout=0.2)
        second = asyncio.create_task(
            service.capture(_request(request_id="capture-second", timeout=0.5))
        )
        await asyncio.sleep(0.02)

        assert adapter.capture_calls == 1
        assert adapter.max_active_captures == 1

        adapter.release_first.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert second_result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert adapter.capture_calls == 2
        assert adapter.max_active_captures == 1
        assert adapter.active_captures == 0

    asyncio.run(scenario())


def test_cancelled_lock_waiter_cannot_run_after_active_capture_releases() -> None:
    async def scenario() -> None:
        adapter = _HeldCaptureAdapter()
        service = MicrophoneCaptureService(adapter)
        first = asyncio.create_task(
            service.capture(_request(request_id="capture-first", timeout=0.5))
        )
        await asyncio.wait_for(adapter.first_started.wait(), timeout=0.2)
        second = asyncio.create_task(
            service.capture(_request(request_id="capture-second", timeout=0.5))
        )
        await asyncio.sleep(0)

        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled lock waiter must propagate cancellation")

        adapter.release_first.set()
        first_result = await asyncio.wait_for(first, timeout=0.2)
        await asyncio.sleep(0)

        assert first_result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
        assert adapter.capture_calls == 1
        assert adapter.max_active_captures == 1
        assert adapter.active_captures == 0

    asyncio.run(scenario())
