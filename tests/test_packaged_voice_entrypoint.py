from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.microphone_capture import (
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.packaging.notices import RUNTIME_DISTRIBUTIONS
from nika_core.packaging.windows import default_windows_plan
from nika_core.speech_to_text import (
    SpeechToTextAdapterResponse,
    SpeechToTextRequest,
)
from nika_core.ui import packaged_voice
from nika_core.ui.bridge_models import UIResult
from scripts import m11_release, nika_windows


class _FakeVoice:
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


class _FakeMicrophone:
    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        return MicrophoneCaptureCapabilities(
            provider_id="windows-wasapi",
            device_id="wasapi-test-device",
        )

    async def capture(
        self,
        request: MicrophoneCaptureRequest,
    ) -> MicrophoneCaptureResponse:
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
    @classmethod
    def from_whisper_files(cls, **_kwargs: object) -> _FakeStt:
        return _FakeStt()


def test_packaged_voice_is_bounded_unavailable_off_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packaged_voice.sys, "platform", "linux")
    feature = packaged_voice.build_packaged_voice(tmp_path)

    snapshot = feature.snapshot()
    assert snapshot == {
        "schema": "nika.packaged-voice-state:v1",
        "available": False,
        "message": "Голосовий ввід доступний лише у застосунку Windows.",
        "turn": None,
    }
    assert feature.start({}).status == "rejected"
    assert feature.cancel({}).status == "completed"
    feature.close()


def test_packaged_voice_missing_local_model_fails_closed_before_native_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(packaged_voice.sys, "platform", "win32")

    class _MustNotConstruct:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("native microphone must not load without model files")

    monkeypatch.setattr(
        packaged_voice,
        "WindowsWasapiMicrophoneCaptureAdapter",
        _MustNotConstruct,
    )
    feature = packaged_voice.build_packaged_voice(tmp_path)

    snapshot = feature.snapshot()
    assert snapshot["available"] is False
    assert "не встановлена" in str(snapshot["message"])
    assert snapshot["turn"] is None


def test_packaged_voice_builds_canonical_controller_with_local_model_files(
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

    feature = packaged_voice.build_packaged_voice(tmp_path)
    try:
        snapshot = feature.snapshot()
        assert snapshot["available"] is True
        turn = snapshot["turn"]
        assert isinstance(turn, dict)
        assert turn["schema"] == "nika.desktop-voice-state:v1"
        assert turn["status"] == "idle"
        assert turn["transcript"] is None
    finally:
        feature.close()


def test_packaged_bridge_exposes_voice_actions_state_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeVoice()
    monkeypatch.setattr(nika_windows, "build_packaged_voice", lambda _root: fake)
    config = AppConfig(database_path=(tmp_path / "nika.db").resolve())

    session = nika_windows.build_windows_session(config)
    try:
        state = session.bridge.get_state()
        assert state["ok"] is True
        assert state["state"]["voice"] == fake.snapshot()

        start = session.bridge.dispatch(
            {"request_id": "voice-start", "action_id": "voice.start", "payload": {}}
        )
        cancel = session.bridge.dispatch(
            {"request_id": "voice-cancel", "action_id": "voice.cancel", "payload": {}}
        )
        assert start["status"] == "accepted"
        assert cancel["status"] == "completed"
        assert fake.started == 1
        assert fake.cancelled == 1
    finally:
        session.close()
    assert fake.closed is True


def test_packaged_voice_actions_are_registered_without_forced_shortcuts() -> None:
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

    assert 'id="voice-status" role="status" aria-live="polite"' in html
    assert 'data-action-id="voice.start"' in html
    assert 'data-action-id="voice.cancel"' in html
    assert 'id="voice-use-command" disabled' in html
    assert "commandInput.value = voiceTranscriptValue;" in script
    assert 'turn.activated === true' in script
    assert "Перевірте його перед створенням завдання." in script


def test_windows_release_explicitly_packages_voice_dependencies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    m11 = (root / ".github/workflows/m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
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
    for module_name in ("numpy", "sherpa_onnx", "sounddevice"):
        index = args.index(module_name)
        assert args[index - 1] == "--hidden-import"

    assert "numpy" in RUNTIME_DISTRIBUTIONS
    assert "sherpa-onnx" in RUNTIME_DISTRIBUTIONS
    assert "sounddevice" in RUNTIME_DISTRIBUTIONS


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
  "sounddevice_imported": true,
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
    target = m11_release.prove_packaged_voice_runtime(
        bundle,
        source_sha=source_sha,
    )

    assert calls and calls[0][0] == str(executable)
    assert "--voice-runtime-proof" in calls[0]
    evidence = target.read_text(encoding="utf-8")
    assert source_sha in evidence
    assert '"packaged_executable_proven": true' in evidence
    assert '"microphone_opened": false' in evidence
    assert '"model_loaded": false' in evidence
