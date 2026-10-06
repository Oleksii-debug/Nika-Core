from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.microphone_capture import MicrophoneCaptureCapabilities
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.packaging.notices import RUNTIME_DISTRIBUTIONS
from nika_core.packaging.windows import default_windows_plan
from nika_core.speech_to_text import (
    SpeechAudio,
    SpeechAudioFormat,
    SpeechToTextAdapterError,
    SpeechToTextAdapterResponse,
    SpeechToTextRequest,
)
from nika_core.ui import packaged_voice
from nika_core.ui.bridge_models import UIResult
from scripts import m11_release, nika_windows


class _FakeMicrophone:
    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return MicrophoneCaptureCapabilities(
            provider_id="windows-wasapi",
            device_id="wasapi-test-device",
        )

    async def capture(self, _request: object) -> object:
        raise AssertionError("capture must not run while only building packaged voice")


class _FakeStt:
    provider_kind = ProviderKind.LOCAL
    provider_id = "sherpa-onnx-whisper"
    supported_models = ("whisper-local",)

    async def transcribe(
        self,
        request: SpeechToTextRequest,
    ) -> SpeechToTextAdapterResponse:
        raise AssertionError("transcribe must not run while only building packaged voice")


class _FakeSherpaFactory:
    calls = 0

    @classmethod
    def from_whisper_files(cls, **_kwargs: object) -> _FakeStt:
        cls.calls += 1
        return _FakeStt()


class _FakeVoice:
    available = True

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0
        self.closed = False

    def start(self, payload: dict[str, Any]) -> UIResult:
        assert payload == {}
        self.started += 1
        return UIResult(
            request_id="desktop-handler",
            status="accepted",
            message="voice accepted",
        )

    def cancel(self, payload: dict[str, Any]) -> UIResult:
        assert payload == {}
        self.cancelled += 1
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="voice cancelled",
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": "nika.packaged-voice-state:v1",
            "available": False,
            "message": "voice test state",
            "turn": None,
        }

    def close(self) -> None:
        self.closed = True


def test_packaged_voice_is_bounded_unavailable_off_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packaged_voice.sys, "platform", "linux")
    feature = packaged_voice.build_packaged_voice(tmp_path.resolve())

    assert feature.snapshot() == {
        "schema": "nika.packaged-voice-state:v1",
        "available": False,
        "message": "Голосовий ввід доступний лише у застосунку Windows.",
        "turn": None,
    }
    assert feature.start({}).status == "rejected"
    assert feature.cancel({}).status == "completed"
    with pytest.raises(ValueError, match="does not accept payload authority"):
        feature.start({"unexpected": True})


def test_packaged_voice_build_is_lazy_and_preserves_current_vad_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_root = tmp_path / "voice" / "whisper"
    model_root.mkdir(parents=True)
    for name in ("encoder.onnx", "decoder.onnx", "tokens.txt"):
        (model_root / name).write_bytes(b"test")
    monkeypatch.setattr(packaged_voice.sys, "platform", "win32")
    monkeypatch.setattr(
        packaged_voice,
        "WindowsWasapiMicrophoneCaptureAdapter",
        _FakeMicrophone,
    )
    monkeypatch.setattr(
        packaged_voice,
        "SherpaOnnxWhisperSpeechToTextAdapter",
        _FakeSherpaFactory,
    )

    def unexpected_submit(_coroutine: Any) -> Any:
        raise AssertionError("building packaged voice must not submit a turn")

    _FakeSherpaFactory.calls = 0
    feature = packaged_voice.build_packaged_voice(
        tmp_path.resolve(),
        submit=unexpected_submit,
    )
    try:
        assert _FakeSherpaFactory.calls == 0
        assert feature.available is True
        controller = feature._controller
        assert controller is not None
        assert controller._service._enable_voice_activity is True
        turn = feature.snapshot()["turn"]
        assert isinstance(turn, dict)
        assert turn["status"] == "idle"
    finally:
        feature.close()


def _lazy_test_request() -> SpeechToTextRequest:
    return SpeechToTextRequest(
        request_id="lazy-load-test",
        provider_id="sherpa-onnx-whisper",
        model="whisper-local",
        audio=SpeechAudio(
            data=b"\x00\x00",
            audio_format=SpeechAudioFormat.PCM_S16LE,
            sample_rate_hz=16_000,
            channels=1,
        ),
        language="uk",
    )


def test_lazy_whisper_load_reuses_one_inflight_factory_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class _ReadyStt:
        async def transcribe(
            self,
            request: SpeechToTextRequest,
        ) -> SpeechToTextAdapterResponse:
            return SpeechToTextAdapterResponse(
                request_id=request.request_id,
                provider_id=request.provider_id,
                model=request.model,
                text="ніка тест",
            )

    class _BlockingFactory:
        @classmethod
        def from_whisper_files(cls, **_kwargs: object) -> _ReadyStt:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=2)
            return _ReadyStt()

    monkeypatch.setattr(
        packaged_voice,
        "SherpaOnnxWhisperSpeechToTextAdapter",
        _BlockingFactory,
    )
    adapter = packaged_voice._LazySherpaAdapter(
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        tokens=tmp_path / "tokens.txt",
    )

    async def scenario() -> None:
        first = asyncio.create_task(adapter.transcribe(_lazy_test_request()))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(adapter.transcribe(_lazy_test_request()))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        response = await second
        assert response.text == "ніка тест"
        assert calls == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_lazy_whisper_load_failure_allows_one_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class _ReadyStt:
        async def transcribe(
            self,
            request: SpeechToTextRequest,
        ) -> SpeechToTextAdapterResponse:
            return SpeechToTextAdapterResponse(
                request_id=request.request_id,
                provider_id=request.provider_id,
                model=request.model,
                text="повтор успішний",
            )

    class _FlakyFactory:
        @classmethod
        def from_whisper_files(cls, **_kwargs: object) -> _ReadyStt:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic native load failure")
            return _ReadyStt()

    monkeypatch.setattr(
        packaged_voice,
        "SherpaOnnxWhisperSpeechToTextAdapter",
        _FlakyFactory,
    )
    adapter = packaged_voice._LazySherpaAdapter(
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        tokens=tmp_path / "tokens.txt",
    )

    async def scenario() -> None:
        with pytest.raises(SpeechToTextAdapterError):
            await adapter.transcribe(_lazy_test_request())
        response = await adapter.transcribe(_lazy_test_request())
        assert response.text == "повтор успішний"
        assert calls == 2

    asyncio.run(scenario())


def test_current_windows_bridge_exposes_voice_state_actions_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeVoice()
    captured_submit: list[Any] = []

    def fake_build(_root: Path, *, submit: Any = None) -> _FakeVoice:
        captured_submit.append(submit)
        return fake

    monkeypatch.setattr(nika_windows, "build_packaged_voice", fake_build)
    cleanup_callbacks: list[Any] = []
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=(tmp_path / "nika.db").resolve()),
        start_startup_recovery=False,
        register_cleanup=cleanup_callbacks.append,
    )

    state = bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["voice"] == fake.snapshot()
    assert len(captured_submit) == 1
    assert callable(captured_submit[0])

    start = bridge.dispatch(
        {"request_id": "voice-start", "action_id": "voice.start", "payload": {}}
    )
    cancel = bridge.dispatch(
        {"request_id": "voice-cancel", "action_id": "voice.cancel", "payload": {}}
    )
    assert start["status"] == "accepted"
    assert cancel["status"] == "completed"
    assert fake.started == 1
    assert fake.cancelled == 1
    assert fake.close in cleanup_callbacks

    for cleanup in reversed(cleanup_callbacks):
        cleanup()
    assert fake.closed is True


def test_packaged_voice_actions_have_no_forced_shortcuts() -> None:
    actions = build_default_action_registry()
    start = actions.get("voice.start")
    cancel = actions.get("voice.cancel")

    assert start.label == "Почати голосовий ввід"
    assert cancel.label == "Скасувати голосовий ввід"
    assert start.default_binding is None
    assert cancel.default_binding is None


def test_packaged_voice_ui_requires_manual_transcript_staging() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    script = (root / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")

    assert '<h2 id="voice-heading" tabindex="-1">Голосовий ввід</h2>' in html
    assert html.count('role="status"') == 1
    assert html.count('aria-live="polite"') == 1
    assert 'data-action-id="voice.start"' in html
    assert 'data-action-id="voice.cancel"' in html
    assert 'id="voice-use-command"' in html
    assert "renderVoice(state.voice ?? null);" in script
    assert "commandInput.value = voiceTranscriptValue;" in script
    assert 'turn.activated === true' in script
    assert "Перевірте його перед створенням завдання." in script


def test_windows_release_packages_voice_dependencies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    m11 = (root / ".github/workflows/m11-windows-release.yml").read_text(encoding="utf-8")
    m12 = (root / ".github/workflows/m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )
    assert '.[gui,voice,qa,dev]' in m11
    assert '.[gui,voice,qa,dev]' in m12

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>\n", encoding="utf-8")
    args = default_windows_plan(tmp_path).pyinstaller_args()
    for module_name in ("numpy", "sounddevice"):
        index = args.index(module_name)
        assert args[index - 1] == "--hidden-import"
    for package_name in ("sherpa_onnx", "_sounddevice_data"):
        index = args.index(package_name)
        assert args[index - 1] == "--collect-all"

    assert {"numpy", "sherpa-onnx", "sherpa-onnx-core", "sounddevice"} <= set(
        RUNTIME_DISTRIBUTIONS
    )


def test_release_proves_voice_dependencies_through_frozen_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    executable = bundle / "NikaCore.exe"
    executable.write_bytes(b"synthetic executable")
    source_sha = "0123456789abcdef0123456789abcdef01234567"
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        check: bool,
        timeout: int,
    ) -> Any:
        assert check is False
        assert timeout == 30
        calls.append(args)
        output = Path(args[args.index("--voice-runtime-proof-output") + 1])
        output.write_text(
            """{
  "schema": "nika.packaged-voice-runtime-proof:v1",
  "numpy_imported": true,
  "sherpa_onnx_imported": true,
  "sherpa_native_imported": true,
  "sounddevice_imported": true,
  "sounddevice_data_proven": true,
  "microphone_opened": false,
  "model_loaded": false,
  "human_tested": false,
  "nvda_verified": false,
  "production_release_ready": false
}
""",
            encoding="utf-8",
        )
        return m11_release.subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(m11_release.subprocess, "run", fake_run)
    target = m11_release.prove_packaged_voice_runtime(bundle, source_sha=source_sha)

    assert calls and calls[0][0] == str(executable)
    assert "--voice-runtime-proof" in calls[0]
    evidence = target.read_text(encoding="utf-8")
    assert source_sha in evidence
    assert '"packaged_executable_proven": true' in evidence
    assert '"microphone_opened": false' in evidence
    assert '"model_loaded": false' in evidence
