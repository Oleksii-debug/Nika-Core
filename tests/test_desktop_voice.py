from __future__ import annotations

import asyncio
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
from nika_core.ui.desktop_voice import DesktopVoiceStatus, DesktopVoiceTurnController
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
        with pytest.raises(ValueError, match="уже виконується"):
            controller.start({})
        assert controller.cancel({}).status == "accepted"
        _wait_status(controller, DesktopVoiceStatus.CANCELLED)
    finally:
        submitter.close()


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
    assert "доказові" in snapshot.message


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
