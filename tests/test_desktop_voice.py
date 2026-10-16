from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap
from nika_core.microphone_capture import (
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
    MicrophoneCaptureService,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.speech_to_text import (
    SpeechToTextAdapterResponse,
    SpeechToTextRequest,
    SpeechToTextService,
)
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_voice import (
    DesktopVoiceSnapshot,
    DesktopVoiceStatus,
    DesktopVoiceTurnController,
)
from nika_core.voice_turn import OneShotVoiceTurnService, VoiceTurnRequest, VoiceTurnResult
from nika_core.wake_activation import WakeActivationDetector


class _LoopSubmitter:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(timeout=2)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(
        self,
        coroutine: Coroutine[Any, Any, Any],
    ) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        assert self.thread.is_alive() is False
        self.loop.close()


class _MicrophoneAdapter:
    def __init__(self, *, transcript_gate: threading.Event | None = None) -> None:
        self.calls = 0
        self.transcript_gate = transcript_gate
        self.cancelled = threading.Event()
        self._capabilities = MicrophoneCaptureCapabilities(
            provider_id="test-microphone",
            device_id="test-device",
        )

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return self._capabilities

    async def capture(
        self,
        request: MicrophoneCaptureRequest,
    ) -> MicrophoneCaptureResponse:
        self.calls += 1
        if self.transcript_gate is not None:
            self.transcript_gate.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=b"\x01\x00" * request.sample_count,
            latency_ms=1.0,
        )


class _SttAdapter:
    provider_kind = ProviderKind.LOCAL
    provider_id = "test-stt"
    supported_models = ("test-model",)

    def __init__(self, text: str = "ніка виконай команду") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        self.calls += 1
        return SpeechToTextAdapterResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            model=request.model,
            text=self.text,
            detected_language="uk",
            latency_ms=2.0,
        )


def _service(
    microphone: _MicrophoneAdapter,
    stt: _SttAdapter | None = None,
) -> OneShotVoiceTurnService:
    return OneShotVoiceTurnService(
        microphone=MicrophoneCaptureService(microphone),
        speech_to_text=SpeechToTextService(stt or _SttAdapter()),
        wake_detector=WakeActivationDetector(),
    )


def _request(request_id: str) -> VoiceTurnRequest:
    return VoiceTurnRequest(
        request_id=request_id,
        capture=MicrophoneCaptureRequest(
            request_id=request_id,
            provider_id="test-microphone",
            device_id="test-device",
            sample_rate_hz=16_000,
            sample_count=16,
        ),
        stt_provider_id="test-stt",
        stt_model="test-model",
        language="uk",
    )


def _wait_status(
    controller: DesktopVoiceTurnController,
    status: DesktopVoiceStatus,
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = controller.snapshot()
        if snapshot["status"] == status.value:
            return snapshot
        time.sleep(0.01)
    snapshot = controller.snapshot()
    assert snapshot["status"] == status.value
    return snapshot


def test_start_returns_immediately_and_projects_bounded_success_state() -> None:
    submitter = _LoopSubmitter()
    microphone = _MicrophoneAdapter()
    factory_threads: list[int] = []
    ui_thread = threading.get_ident()

    def factory(request_id: str) -> VoiceTurnRequest:
        factory_threads.append(threading.get_ident())
        return _request(request_id)

    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=factory,
        submit=submitter.submit,
    )
    try:
        started_at = time.monotonic()
        result = controller.start({})
        elapsed = time.monotonic() - started_at

        assert result.status == "accepted"
        assert elapsed < 0.25
        snapshot = _wait_status(controller, DesktopVoiceStatus.COMPLETED)
        assert snapshot["activated"] is True
        assert snapshot["transcript"] == "ніка виконай команду"
        assert snapshot["active"] is False
        assert "evidence" not in snapshot
        assert "pcm" not in snapshot
        assert microphone.calls == 1
        assert factory_threads
        assert factory_threads[0] != ui_thread
    finally:
        submitter.close()


def test_behavioral_future_subclass_is_rejected_before_callback_registration() -> None:
    class BehavioralFuture(Future[Any]):
        def add_done_callback(self, fn: Any) -> None:
            raise AssertionError("behavioral Future callback must not execute")

        def cancel(self) -> bool:
            raise AssertionError("behavioral Future cancel must not execute")

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        return BehavioralFuture()

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submit,
    )

    with pytest.raises(TypeError, match="concurrent.futures.Future"):
        controller.start({})

    assert controller.snapshot()["status"] == DesktopVoiceStatus.FAILED.value


def test_submitter_can_reenter_snapshot_without_controller_deadlock() -> None:
    holder: dict[str, DesktopVoiceTurnController] = {}

    def reentrant_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        snapshot = holder["controller"].snapshot()
        assert snapshot["status"] == DesktopVoiceStatus.RUNNING.value
        assert snapshot["active"] is True
        coroutine.close()
        future: Future[Any] = Future()
        future.set_exception(RuntimeError("synthetic submit completion"))
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=reentrant_submit,
    )
    holder["controller"] = controller

    started_at = time.monotonic()
    result = controller.start({})
    elapsed = time.monotonic() - started_at

    assert result.status == "accepted"
    assert elapsed < 0.25
    snapshot = controller.snapshot()
    assert snapshot["status"] == DesktopVoiceStatus.FAILED.value
    assert snapshot["active"] is False


def test_cancel_during_blocking_submit_is_propagated_after_future_binding() -> None:
    entered = threading.Event()
    release = threading.Event()
    returned: list[UIResult] = []

    def blocking_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        entered.set()
        assert release.wait(timeout=2)
        coroutine.close()
        return Future()

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=blocking_submit,
    )

    thread = threading.Thread(
        target=lambda: returned.append(controller.start({})),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=2)

    cancel_result = controller.cancel({})
    assert cancel_result.status == "accepted"
    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True
    with pytest.raises(ValueError, match="завершує скасування"):
        controller.start({})

    release.set()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert returned and returned[0].status == "accepted"
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False


def test_close_during_blocking_submit_marks_cancellation_and_fails_closed() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        entered.set()
        assert release.wait(timeout=2)
        coroutine.close()
        return Future()

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=blocking_submit,
    )
    thread = threading.Thread(target=lambda: controller.start({}), daemon=True)
    thread.start()
    assert entered.wait(timeout=2)

    with pytest.raises(RuntimeError, match="voice submission did not settle"):
        controller.close(timeout_seconds=0.01)
    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True

    release.set()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False


def test_submit_base_exception_cleans_reservation_and_allows_reentry() -> None:
    attempts = 0

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt("synthetic submit base exception")
        coroutine.close()
        future: Future[Any] = Future()
        future.set_exception(RuntimeError("synthetic retry completion"))
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submit,
    )

    with pytest.raises(KeyboardInterrupt, match="synthetic submit base exception"):
        controller.start({})

    failed = controller.snapshot()
    assert failed["status"] == DesktopVoiceStatus.FAILED.value
    assert failed["active"] is False

    assert controller.start({}).status == "accepted"
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.FAILED.value
    assert final["active"] is False


def test_precompleted_future_callback_does_not_deadlock_start() -> None:
    def immediate_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        future.set_exception(RuntimeError("synthetic immediate failure"))
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=immediate_submit,
    )

    started_at = time.monotonic()
    result = controller.start({})
    elapsed = time.monotonic() - started_at

    assert result.status == "accepted"
    assert elapsed < 0.25
    assert controller.snapshot()["status"] == DesktopVoiceStatus.FAILED.value


def test_precompleted_base_exception_future_cannot_strand_controller() -> None:
    def immediate_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        future.set_exception(KeyboardInterrupt("synthetic async base exception"))
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=immediate_submit,
    )

    assert controller.start({}).status == "accepted"
    snapshot = controller.snapshot()
    assert snapshot["status"] == DesktopVoiceStatus.FAILED.value
    assert snapshot["active"] is False
    assert snapshot["transcript"] is None


def test_cancel_callback_does_not_deadlock_controller_lock() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    microphone = _MicrophoneAdapter(transcript_gate=entered)
    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        controller.start({})
        assert entered.wait(timeout=2)

        done = threading.Event()
        outcome: list[str] = []

        def cancel_from_thread() -> None:
            outcome.append(controller.cancel({}).status)
            done.set()

        thread = threading.Thread(target=cancel_from_thread, daemon=True)
        thread.start()
        assert done.wait(timeout=1), "cancel deadlocked while Future callback reacquired lock"
        thread.join(timeout=1)
        assert thread.is_alive() is False
        assert outcome == ["accepted"]
        _wait_status(controller, DesktopVoiceStatus.CANCELLED)
    finally:
        submitter.close()


def test_second_turn_is_rejected_while_first_turn_is_active() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    microphone = _MicrophoneAdapter(transcript_gate=entered)
    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        assert controller.start({}).status == "accepted"
        assert entered.wait(timeout=2)
        with pytest.raises(ValueError, match="виконується або завершує скасування"):
            controller.start({})
        assert controller.cancel({}).status == "accepted"
        _wait_status(controller, DesktopVoiceStatus.CANCELLED)
    finally:
        submitter.close()


def test_cancelled_outer_future_does_not_clear_active_before_coroutine_settles() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    active: Future[VoiceTurnResult] = Future()
    started = threading.Event()
    settled = threading.Event()
    started.set()

    with controller._lock:
        controller._active = active
        controller._active_started = started
        controller._active_settled = settled
        controller._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.RUNNING,
            request_id="desktop-voice-settlement",
            message="running",
        )

    assert active.cancel() is True
    controller._finish("desktop-voice-settlement", active)

    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True
    with pytest.raises(ValueError, match="завершує скасування"):
        controller.start({})

    settled.set()
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False

    submitter.close()


def test_cancel_waits_for_blocking_request_factory_before_reentry() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    release = threading.Event()

    def blocking_factory(request_id: str) -> VoiceTurnRequest:
        entered.set()
        assert release.wait(timeout=2)
        return _request(request_id)

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=blocking_factory,
        submit=submitter.submit,
    )
    try:
        assert controller.start({}).status == "accepted"
        assert entered.wait(timeout=2)
        assert controller.cancel({}).status == "accepted"
        pending = controller.snapshot()
        assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
        assert pending["active"] is True
        with pytest.raises(ValueError, match="завершує скасування"):
            controller.start({})

        release.set()
        final = _wait_status(controller, DesktopVoiceStatus.CANCELLED)
        assert final["active"] is False
    finally:
        release.set()
        submitter.close()


def test_cancelled_late_factory_base_exception_cannot_strand_controller() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    release = threading.Event()

    def failing_factory(_request_id: str) -> VoiceTurnRequest:
        entered.set()
        assert release.wait(timeout=2)
        raise KeyboardInterrupt("synthetic worker base exception")

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=failing_factory,
        submit=submitter.submit,
    )
    try:
        assert controller.start({}).status == "accepted"
        assert entered.wait(timeout=2)
        assert controller.cancel({}).status == "accepted"
        assert controller.snapshot()["status"] == DesktopVoiceStatus.CANCELLING.value

        release.set()
        final = _wait_status(controller, DesktopVoiceStatus.CANCELLED)
        assert final["active"] is False
        assert final["transcript"] is None
    finally:
        release.set()
        submitter.close()


def test_cancel_running_future_masks_late_success_when_runtime_refuses_cancel() -> None:
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=lambda coroutine: Future(),  # pragma: no cover - not used
    )
    active: Future[VoiceTurnResult] = Future()
    assert active.set_running_or_notify_cancel() is True
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-running-cancel"))
    )

    with controller._lock:
        controller._active = active
        controller._active_started = threading.Event()
        controller._active_settled = threading.Event()
        controller._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.RUNNING,
            request_id="desktop-voice-running-cancel",
            message="running",
        )
    active.add_done_callback(
        lambda done: controller._finish("desktop-voice-running-cancel", done)
    )

    cancel_result = controller.cancel({})
    assert cancel_result.status == "accepted"
    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True

    active.set_result(result)
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False
    assert final["transcript"] is None


def test_close_running_future_masks_late_success_after_shutdown_timeout() -> None:
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=lambda coroutine: Future(),  # pragma: no cover - not used
    )
    active: Future[VoiceTurnResult] = Future()
    assert active.set_running_or_notify_cancel() is True
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-running-close"))
    )

    with controller._lock:
        controller._active = active
        controller._active_started = threading.Event()
        controller._active_settled = threading.Event()
        controller._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.RUNNING,
            request_id="desktop-voice-running-close",
            message="running",
        )
    active.add_done_callback(
        lambda done: controller._finish("desktop-voice-running-close", done)
    )

    with pytest.raises(RuntimeError, match="remained active"):
        controller.close(timeout_seconds=0.01)

    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True
    assert pending["transcript"] is None

    active.set_result(result)
    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False
    assert final["transcript"] is None


def test_cancel_propagates_to_active_async_capture() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    microphone = _MicrophoneAdapter(transcript_gate=entered)
    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        controller.start({})
        assert entered.wait(timeout=2)
        result = controller.cancel({})
        assert result.status == "accepted"
        snapshot = _wait_status(controller, DesktopVoiceStatus.CANCELLED)
        assert snapshot["transcript"] is None
        assert microphone.cancelled.wait(timeout=2)
    finally:
        submitter.close()


def test_close_waits_for_submission_then_cancels_resulting_future() -> None:
    entered = threading.Event()
    release = threading.Event()
    future_created = threading.Event()
    future: Future[Any] = Future()

    def blocking_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        entered.set()
        assert release.wait(timeout=2)
        coroutine.close()
        future_created.set()
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=blocking_submit,
    )
    start_done = threading.Event()

    def start_voice() -> None:
        try:
            assert controller.start({}).status == "accepted"
        finally:
            start_done.set()

    starter = threading.Thread(target=start_voice, daemon=True)
    starter.start()
    assert entered.wait(timeout=2)

    close_done = threading.Event()
    close_errors: list[BaseException] = []

    def close_voice() -> None:
        try:
            controller.close(timeout_seconds=1.0)
        except RuntimeError as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    closer = threading.Thread(target=close_voice, daemon=True)
    closer.start()
    time.sleep(0.05)
    assert close_done.is_set() is False
    assert controller.snapshot()["status"] == DesktopVoiceStatus.CANCELLING.value

    release.set()
    assert future_created.wait(timeout=2)
    assert close_done.wait(timeout=2)
    assert start_done.wait(timeout=2)
    starter.join(timeout=1)
    closer.join(timeout=1)

    assert close_errors == []
    assert future.cancelled() is True
    snapshot = controller.snapshot()
    assert snapshot["status"] == DesktopVoiceStatus.CANCELLED.value
    assert snapshot["active"] is False


def test_close_submission_wait_respects_deadline_without_losing_reservation() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        entered.set()
        assert release.wait(timeout=2)
        coroutine.close()
        future: Future[Any] = Future()
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=blocking_submit,
    )

    starter = threading.Thread(target=lambda: controller.start({}), daemon=True)
    starter.start()
    assert entered.wait(timeout=2)

    started_at = time.monotonic()
    with pytest.raises(RuntimeError, match="submission did not settle"):
        controller.close(timeout_seconds=0.05)
    elapsed = time.monotonic() - started_at

    assert elapsed >= 0.04
    snapshot = controller.snapshot()
    assert snapshot["status"] == DesktopVoiceStatus.CANCELLING.value
    assert snapshot["active"] is True
    with pytest.raises(ValueError, match="завершує скасування"):
        controller.start({})

    release.set()
    starter.join(timeout=2)
    assert starter.is_alive() is False
    _wait_status(controller, DesktopVoiceStatus.CANCELLED)


def test_close_cancels_active_turn_and_waits_for_coroutine_settlement() -> None:
    submitter = _LoopSubmitter()
    entered = threading.Event()
    microphone = _MicrophoneAdapter(transcript_gate=entered)
    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        controller.start({})
        assert entered.wait(timeout=2)
        controller.close(timeout_seconds=1.0)

        snapshot = controller.snapshot()
        assert snapshot["status"] == DesktopVoiceStatus.CANCELLED.value
        assert snapshot["active"] is False
        assert microphone.cancelled.wait(timeout=2)
    finally:
        submitter.close()


def test_close_fails_closed_when_started_coroutine_does_not_settle() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    active: Future[VoiceTurnResult] = Future()
    started = threading.Event()
    settled = threading.Event()
    started.set()

    with controller._lock:
        controller._active = active
        controller._active_started = started
        controller._active_settled = settled
        controller._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.RUNNING,
            request_id="desktop-voice-close-timeout",
            message="running",
        )
    active.add_done_callback(
        lambda done: controller._finish("desktop-voice-close-timeout", done)
    )

    with pytest.raises(RuntimeError, match="did not settle"):
        controller.close(timeout_seconds=0.01)

    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True

    settled.set()
    assert controller.snapshot()["status"] == DesktopVoiceStatus.CANCELLED.value
    controller.close(timeout_seconds=0.01)
    submitter.close()


def test_close_fails_closed_if_future_is_running_before_coroutine_start() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    active: Future[VoiceTurnResult] = Future()
    assert active.set_running_or_notify_cancel() is True

    with controller._lock:
        controller._active = active
        controller._active_started = threading.Event()
        controller._active_settled = threading.Event()
        controller._snapshot = DesktopVoiceSnapshot(
            status=DesktopVoiceStatus.RUNNING,
            request_id="desktop-voice-close-running",
            message="running",
        )

    try:
        with pytest.raises(RuntimeError, match="remained active"):
            controller.close(timeout_seconds=0.01)
        snapshot = controller.snapshot()
        assert snapshot["status"] == DesktopVoiceStatus.CANCELLING.value
        assert snapshot["active"] is True
    finally:
        active.set_exception(RuntimeError("synthetic shutdown release"))
        controller._finish("desktop-voice-close-running", active)
        submitter.close()


def test_close_rejects_behavioral_or_unbounded_timeout_before_cancel() -> None:
    class BehavioralFloat(float):
        def __float__(self) -> float:
            raise AssertionError("behavioral timeout conversion must not execute")

    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        for value, error in (
            (BehavioralFloat(1.0), TypeError),
            (float("nan"), ValueError),
            (float("inf"), ValueError),
            (0.0, ValueError),
            (30.1, ValueError),
        ):
            with pytest.raises(error):
                controller.close(timeout_seconds=value)  # type: ignore[arg-type]
        assert controller.snapshot()["status"] == DesktopVoiceStatus.IDLE.value
    finally:
        submitter.close()


def test_cancel_is_idempotent_when_no_turn_is_active() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        result = controller.cancel({})
        assert result.status == "completed"
        assert "немає" in result.message
        assert controller.snapshot()["status"] == DesktopVoiceStatus.IDLE.value
    finally:
        submitter.close()


def test_request_factory_cannot_change_generated_identity() -> None:
    submitter = _LoopSubmitter()
    microphone = _MicrophoneAdapter()

    def forged_factory(_request_id: str) -> VoiceTurnRequest:
        return _request("forged-request-id")

    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=forged_factory,
        submit=submitter.submit,
    )
    try:
        assert controller.start({}).status == "accepted"
        snapshot = _wait_status(controller, DesktopVoiceStatus.FAILED)
        assert snapshot["transcript"] is None
        assert microphone.calls == 0
    finally:
        submitter.close()


def test_request_factory_must_return_exact_voice_request() -> None:
    submitter = _LoopSubmitter()
    microphone = _MicrophoneAdapter()
    controller = DesktopVoiceTurnController(
        service=_service(microphone),
        request_factory=lambda _request_id: object(),  # type: ignore[arg-type]
        submit=submitter.submit,
    )
    try:
        assert controller.start({}).status == "accepted"
        _wait_status(controller, DesktopVoiceStatus.FAILED)
        assert microphone.calls == 0
    finally:
        submitter.close()


def test_controller_composes_with_real_synchronous_ui_bridge(tmp_path: Any) -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    actions = ActionRegistry()
    actions.register(
        ActionDefinition(
            "voice.turn.start",
            "Почати голосовий ввід",
            "Voice",
            None,
        )
    )
    actions.register(
        ActionDefinition(
            "voice.turn.cancel",
            "Скасувати голосовий ввід",
            "Voice",
            None,
        )
    )
    bridge = UIActionBridge(
        actions,
        Keymap(store, actions),
        handlers={
            "voice.turn.start": controller.start,
            "voice.turn.cancel": controller.cancel,
        },
        state_provider=lambda: {"voice_turn": controller.snapshot()},
    )
    try:
        started = bridge.dispatch(
            {
                "request_id": "voice-ui-start",
                "action_id": "voice.turn.start",
                "payload": {},
            }
        )
        assert started["status"] == "accepted"
        snapshot = _wait_status(controller, DesktopVoiceStatus.COMPLETED)
        assert snapshot["transcript"] == "ніка виконай команду"

        state = bridge.get_state()
        assert state["ok"] is True
        assert state["state"]["voice_turn"]["status"] == "completed"
        assert state["state"]["voice_turn"]["activated"] is True

        rejected = bridge.dispatch(
            {
                "request_id": "voice-ui-forged",
                "action_id": "voice.turn.start",
                "payload": {"model": "forged"},
            }
        )
        assert rejected["status"] == "rejected"
        assert "payload authority" in rejected["message"]
    finally:
        submitter.close()



def test_behavioral_factory_request_id_fails_without_comparison_dispatch() -> None:
    class BehavioralStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("factory request identity equality must not execute")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("factory request identity inequality must not execute")

    def forged_request(request_id: str) -> VoiceTurnRequest:
        request = _request(request_id)
        object.__setattr__(request, "request_id", BehavioralStr(request_id))
        return request

    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=forged_request,
        submit=submitter.submit,
    )
    try:
        controller.start({})
        snapshot = _wait_status(controller, DesktopVoiceStatus.FAILED)
        assert snapshot["active"] is False
        assert snapshot["transcript"] is None
    finally:
        submitter.close()



def test_action_payload_cannot_supply_hidden_voice_authority() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        with pytest.raises(ValueError, match="does not accept payload authority"):
            controller.start({"model": "forged-model"})
        with pytest.raises(ValueError, match="does not accept payload authority"):
            controller.cancel({"request_id": "other"})
    finally:
        submitter.close()



@pytest.mark.parametrize(
    ("status", "capture_succeeded", "transcription_succeeded"),
    [
        ("capture_failed", True, None),
        ("transcription_failed", True, True),
    ],
)
def test_failure_snapshot_rejects_impossible_component_success(
    status: str,
    capture_succeeded: bool,
    transcription_succeeded: bool | None,
) -> None:
    del capture_succeeded
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    if status == "capture_failed":
        object.__setattr__(
            result.evidence,
            "status",
            type(result.evidence.status).CAPTURE_FAILED,
        )
        object.__setattr__(result, "transcript", None)
        object.__setattr__(result.evidence, "transcription", None)
        object.__setattr__(result.evidence, "wake", None)
        object.__setattr__(result.evidence, "activated", False)
    else:
        assert transcription_succeeded is True
        object.__setattr__(
            result.evidence,
            "status",
            type(result.evidence.status).TRANSCRIPTION_FAILED,
        )
        object.__setattr__(result, "transcript", None)
        object.__setattr__(result.evidence, "wake", None)
        object.__setattr__(result.evidence, "activated", False)

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "failure evidence" in snapshot.message



def test_forged_nested_voice_evidence_fails_closed_without_callback_error() -> None:
    forged = VoiceTurnResult(
        transcript="secret transcript",
        evidence=object(),  # type: ignore[arg-type]
    )

    snapshot = DesktopVoiceTurnController._result_snapshot("desktop-voice-test", forged)

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "evidence" in snapshot.message


def test_forged_exact_success_result_cannot_project_unbound_transcript() -> None:
    async def scenario() -> VoiceTurnResult:
        return await _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))

    result = asyncio.run(scenario())
    object.__setattr__(result, "transcript", "forged transcript")

    snapshot = DesktopVoiceTurnController._result_snapshot("desktop-voice-test", result)

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "неузгоджен" in snapshot.message


def test_forged_success_transcript_length_evidence_cannot_project() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(result.evidence.transcription, "transcript_chars", 1)

    snapshot = DesktopVoiceTurnController._result_snapshot("desktop-voice-test", result)

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_forged_exact_success_result_rejects_noncanonical_wake_outcome() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.wake is not None
    object.__setattr__(result.evidence.wake, "outcome", "detected")
    object.__setattr__(result.evidence, "activated", False)

    snapshot = DesktopVoiceTurnController._result_snapshot("desktop-voice-test", result)

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_forged_exact_success_result_cannot_project_unbound_stt_digest() -> None:
    async def scenario() -> VoiceTurnResult:
        return await _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))

    result = asyncio.run(scenario())
    assert result.evidence.transcription is not None
    object.__setattr__(result.evidence.transcription, "transcript_sha256", "0" * 64)

    snapshot = DesktopVoiceTurnController._result_snapshot("desktop-voice-test", result)

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "доказові" in snapshot.message


def test_behavioral_mapping_payload_is_rejected_without_mapping_methods() -> None:
    class BehavioralDict(dict[str, object]):
        def __bool__(self) -> bool:
            raise AssertionError("payload truthiness must not execute")

    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        with pytest.raises(TypeError, match="exact dict"):
            controller.start(BehavioralDict())
    finally:
        submitter.close()


def test_non_wake_transcript_is_completed_without_activation() -> None:
    submitter = _LoopSubmitter()
    controller = DesktopVoiceTurnController(
        service=_service(
            _MicrophoneAdapter(),
            _SttAdapter(text="сьогодні гарна погода"),
        ),
        request_factory=_request,
        submit=submitter.submit,
    )
    try:
        controller.start({})
        snapshot = _wait_status(controller, DesktopVoiceStatus.COMPLETED)
        assert snapshot["activated"] is False
        assert snapshot["transcript"] == "сьогодні гарна погода"
        assert "не виявлено" in str(snapshot["message"])
    finally:
        submitter.close()


def test_behavioral_nested_evidence_scalars_fail_closed_without_dispatch() -> None:
    class BehavioralStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral evidence equality must not execute")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral evidence inequality must not execute")

    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    object.__setattr__(
        result.evidence.capture,
        "request_id",
        BehavioralStr("desktop-voice-test"),
    )

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


@pytest.mark.parametrize("component", ["transcription", "wake"])
def test_behavioral_completed_request_ids_fail_closed_without_dispatch(
    component: str,
) -> None:
    class BehavioralStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral request-id equality must not execute")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral request-id inequality must not execute")

    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    target = getattr(result.evidence, component)
    assert target is not None
    object.__setattr__(
        target,
        "request_id",
        BehavioralStr("desktop-voice-test"),
    )

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_behavioral_nested_digest_fails_closed_without_dispatch() -> None:
    class BehavioralStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral digest equality must not execute")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral digest inequality must not execute")

    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(
        result.evidence.transcription,
        "audio_sha256",
        BehavioralStr(result.evidence.transcription.audio_sha256 or ""),
    )

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_completed_result_rejects_capture_stt_audio_metadata_mismatch() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(
        result.evidence.transcription,
        "audio_bytes",
        result.evidence.capture.audio_byte_count + 2,
    )

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_completed_result_rejects_impossible_detected_wake_span() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.wake is not None
    assert result.evidence.wake.match_end_token_exclusive is not None
    object.__setattr__(
        result.evidence.wake,
        "match_end_token_exclusive",
        result.evidence.wake.token_count + 1,
    )

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


def test_non_hex_audio_digest_cannot_satisfy_success_evidence_binding() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(result.evidence.capture, "audio_sha256", "g" * 64)
    object.__setattr__(result.evidence.transcription, "audio_sha256", "g" * 64)

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None


@pytest.mark.parametrize("transcript", ["valid\x00hidden", "valid\ud800hidden"])
def test_forged_completed_result_rejects_non_public_transcript(
    transcript: str,
) -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    assert result.evidence.wake is not None
    digest = hashlib.sha256(
        transcript.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    object.__setattr__(result, "transcript", transcript)
    object.__setattr__(
        result.evidence.transcription,
        "transcript_sha256",
        digest,
    )
    object.__setattr__(result.evidence.wake, "transcript_sha256", digest)

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None



def test_behavioral_digest_cannot_strand_precompleted_future_callback() -> None:
    class BehavioralStr(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral digest equality must not execute")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral digest inequality must not execute")

    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-forged"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(
        result.evidence.transcription,
        "audio_sha256",
        BehavioralStr(result.evidence.transcription.audio_sha256 or ""),
    )

    def immediate_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        future.set_result(result)
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=immediate_submit,
    )

    assert controller.start({}).status == "accepted"
    snapshot = controller.snapshot()
    assert snapshot["status"] == DesktopVoiceStatus.FAILED.value
    assert snapshot["active"] is False
    assert snapshot["transcript"] is None



def test_invalid_composition_rejects_completed_evidence_relabel() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    assert result.evidence.wake is not None
    object.__setattr__(
        result.evidence,
        "status",
        type(result.evidence.status).INVALID_COMPOSITION,
    )
    object.__setattr__(result, "transcript", None)
    object.__setattr__(result.evidence, "activated", False)

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "failure evidence" in snapshot.message


def test_invalid_composition_rejects_noncanonical_success_digest_carrier() -> None:
    result = asyncio.run(
        _service(_MicrophoneAdapter()).run(_request("desktop-voice-test"))
    )
    assert result.evidence.transcription is not None
    object.__setattr__(
        result.evidence,
        "status",
        type(result.evidence.status).INVALID_COMPOSITION,
    )
    object.__setattr__(result, "transcript", None)
    object.__setattr__(result.evidence, "wake", None)
    object.__setattr__(result.evidence, "activated", False)
    object.__setattr__(result.evidence.transcription, "transcript_sha256", "g" * 64)

    snapshot = DesktopVoiceTurnController._result_snapshot(
        "desktop-voice-test",
        result,
    )

    assert snapshot.status is DesktopVoiceStatus.FAILED
    assert snapshot.transcript is None
    assert "failure evidence" in snapshot.message


def test_cancel_accepted_during_submission_masks_precompleted_success() -> None:
    entered = threading.Event()
    release = threading.Event()
    controller_holder: list[DesktopVoiceTurnController] = []

    def blocking_submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        entered.set()
        assert release.wait(timeout=2)
        coroutine.close()
        controller = controller_holder[0]
        request_id = controller.snapshot()["request_id"]
        assert type(request_id) is str
        result = asyncio.run(
            _service(_MicrophoneAdapter()).run(_request(request_id))
        )
        future: Future[Any] = Future()
        future.set_result(result)
        return future

    controller = DesktopVoiceTurnController(
        service=_service(_MicrophoneAdapter()),
        request_factory=_request,
        submit=blocking_submit,
    )
    controller_holder.append(controller)
    outcome: list[str] = []

    def run_start() -> None:
        outcome.append(controller.start({}).status)

    thread = threading.Thread(target=run_start, daemon=True)
    thread.start()
    assert entered.wait(timeout=2)
    assert controller.cancel({}).status == "accepted"
    pending = controller.snapshot()
    assert pending["status"] == DesktopVoiceStatus.CANCELLING.value
    assert pending["active"] is True

    release.set()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert outcome == ["accepted"]

    final = controller.snapshot()
    assert final["status"] == DesktopVoiceStatus.CANCELLED.value
    assert final["active"] is False
    assert final["transcript"] is None
