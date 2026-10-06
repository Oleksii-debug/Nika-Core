from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

import nika_core.ui.packaged_voice_model_setup as model_setup
from nika_core.config import AppConfig
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.packaged_voice_model_setup import PackagedVoiceModelSetup
from scripts import nika_windows


def _write_source(root: Path) -> Path:
    source = root / "source-model"
    source.mkdir()
    (source / "encoder.onnx").write_bytes(b"encoder")
    (source / "decoder.onnx").write_bytes(b"decoder")
    (source / "tokens.txt").write_bytes("ніка\n".encode("utf-8"))
    return source


def test_voice_model_setup_is_bounded_off_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "linux")
    setup = PackagedVoiceModelSetup(tmp_path)

    snapshot = setup.snapshot()
    result = setup.install({"source_root": str(tmp_path)})

    assert snapshot == {
        "schema": "nika.packaged-voice-model-setup:v1",
        "status": "missing",
        "generation": 0,
        "active": False,
        "installed": False,
        "can_import": False,
        "restart_required": False,
        "message": (
            "Локальна голосова модель ще не встановлена. Вкажіть повний шлях "
            "до папки з encoder.onnx, decoder.onnx і tokens.txt."
        ),
    }
    assert result.status == "rejected"
    assert result.focus_id == "voice-model-source"


def test_voice_model_setup_imports_exact_files_and_requires_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})
    snapshot = setup.snapshot()
    target = data_root / "voice" / "whisper"

    assert result.status == "completed"
    assert result.focus_id == "voice-heading"
    assert snapshot["status"] == "restart_required"
    assert snapshot["installed"] is True
    assert snapshot["can_import"] is False
    assert snapshot["restart_required"] is True
    assert str(source) not in repr(snapshot)
    assert (target / "encoder.onnx").read_bytes() == b"encoder"
    assert (target / "decoder.onnx").read_bytes() == b"decoder"
    assert (target / "tokens.txt").read_bytes() == "ніка\n".encode("utf-8")
    assert (source / "encoder.onnx").read_bytes() == b"encoder"
    assert not list((data_root / "voice").glob(".whisper-import-*"))
    restarted = PackagedVoiceModelSetup(data_root).snapshot()
    assert restarted["status"] == "installed"
    assert restarted["installed"] is True
    assert restarted["restart_required"] is False


def test_voice_model_setup_background_start_is_nonblocking_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    submitted: list[tuple[Coroutine[Any, Any, Any], Future[Any]]] = []

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        future: Future[Any] = Future()
        submitted.append((coroutine, future))
        return future

    setup = PackagedVoiceModelSetup(data_root, submit=submit)

    result = setup.start({"source_root": str(source)})
    active = setup.snapshot()

    assert result.status == "accepted"
    assert active["status"] == "importing"
    assert active["generation"] == 1
    assert active["active"] is True
    assert active["can_import"] is False
    assert len(submitted) == 1

    asyncio.run(submitted[0][0])
    submitted[0][1].set_result(None)
    terminal = setup.snapshot()

    assert terminal["status"] == "restart_required"
    assert terminal["generation"] == 1
    assert terminal["active"] is False
    assert terminal["installed"] is True
    assert str(source) not in repr(terminal)


def test_voice_model_setup_background_unexpected_failure_recovers_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    submitted: list[Coroutine[Any, Any, Any]] = []

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        submitted.append(coroutine)
        return Future()

    setup = PackagedVoiceModelSetup(data_root, submit=submit)

    def fail_unexpectedly(
        _source_text: str,
        *,
        cancel_event: model_setup.Event | None,
    ) -> None:
        del cancel_event
        raise ValueError("PRIVATE WORKER DETAIL")

    monkeypatch.setattr(setup, "_perform_install", fail_unexpectedly)

    started = setup.start({"source_root": str(source)})
    asyncio.run(submitted[0])
    terminal = setup.snapshot()

    assert started.status == "accepted"
    assert terminal["status"] == "failed"
    assert terminal["active"] is False
    assert terminal["can_import"] is True
    assert terminal["restart_required"] is False
    assert "PRIVATE" not in str(terminal["message"])
    assert not (data_root / "voice" / "whisper").exists()


def test_voice_model_setup_rejects_invalid_host_future_without_sticking_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()

    def submit(coroutine: Coroutine[Any, Any, Any]) -> object:
        del coroutine
        return object()

    setup = PackagedVoiceModelSetup(
        data_root,
        submit=submit,  # type: ignore[arg-type]
    )

    result = setup.start({"source_root": str(source)})
    terminal = setup.snapshot()

    assert result.status == "failed"
    assert terminal["status"] == "failed"
    assert terminal["active"] is False
    assert terminal["can_import"] is True
    assert terminal["restart_required"] is False
    assert not (data_root / "voice" / "whisper").exists()


@pytest.mark.parametrize("future_mode", ["failed", "cancelled"])
def test_voice_model_setup_background_host_future_interruption_recovers_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_mode: str,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        if future_mode == "failed":
            future.set_exception(RuntimeError("PRIVATE HOST DETAIL"))
        else:
            assert future.cancel()
        return future

    setup = PackagedVoiceModelSetup(data_root, submit=submit)

    started = setup.start({"source_root": str(source)})
    terminal = setup.snapshot()

    assert started.status == "accepted"
    assert terminal["status"] == "failed"
    assert terminal["active"] is False
    assert terminal["can_import"] is True
    assert terminal["restart_required"] is False
    assert terminal["message"] == "Фоновий імпорт голосової моделі було перервано."
    assert "PRIVATE" not in repr(terminal)
    assert not (data_root / "voice" / "whisper").exists()


@pytest.mark.parametrize("future_mode", ["failed", "cancelled"])
def test_voice_model_setup_host_interruption_after_publish_requires_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_mode: str,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    submitted: list[Coroutine[Any, Any, Any]] = []
    future: Future[Any] = Future()

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        submitted.append(coroutine)
        return future

    setup = PackagedVoiceModelSetup(data_root, submit=submit)
    started = setup.start({"source_root": str(source)})
    assert setup.snapshot()["status"] == "importing"

    target = data_root / "voice" / "whisper"
    target.mkdir(parents=True)
    for name in ("encoder.onnx", "decoder.onnx", "tokens.txt"):
        (target / name).write_bytes(b"published-model")

    if future_mode == "failed":
        future.set_exception(RuntimeError("PRIVATE HOST DETAIL"))
    else:
        assert future.cancel()

    try:
        terminal = setup.snapshot()
    finally:
        submitted[0].close()

    assert started.status == "accepted"
    assert terminal["status"] == "restart_required"
    assert terminal["active"] is False
    assert terminal["installed"] is True
    assert terminal["can_import"] is False
    assert terminal["restart_required"] is True
    assert "PRIVATE" not in repr(terminal)


def test_voice_model_setup_background_cancel_is_cooperative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    submitted: list[Coroutine[Any, Any, Any]] = []

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        submitted.append(coroutine)
        return Future()

    setup = PackagedVoiceModelSetup(data_root, submit=submit)
    started = setup.start({"source_root": str(source)})
    cancelled = setup.cancel({})
    cancelling = setup.snapshot()
    duplicate = setup.cancel({})

    assert started.status == "accepted"
    assert cancelled.status == "completed"
    assert cancelling["status"] == "cancelling"
    assert cancelling["active"] is True
    assert duplicate.message == "Скасування імпорту голосової моделі вже запитано."

    asyncio.run(submitted[0])
    terminal = setup.snapshot()

    assert terminal["status"] == "cancelled"
    assert terminal["active"] is False
    assert terminal["can_import"] is True
    assert not (data_root / "voice" / "whisper").exists()
    assert not list((data_root / "voice").glob(".whisper-import-*"))


def test_voice_model_setup_existing_install_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    target = data_root / "voice" / "whisper"
    target.mkdir(parents=True)
    for name in ("encoder.onnx", "decoder.onnx", "tokens.txt"):
        (target / name).write_bytes(b"installed")
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "completed"
    assert result.message == "Локальна голосова модель уже встановлена."
    assert setup.snapshot()["status"] == "installed"
    assert (target / "encoder.onnx").read_bytes() == b"installed"


def test_voice_model_setup_refuses_partial_canonical_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    target = data_root / "voice" / "whisper"
    target.mkdir(parents=True)
    (target / "encoder.onnx").write_bytes(b"partial")
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "rejected"
    assert setup.snapshot()["status"] == "partial"
    assert not (target / "decoder.onnx").exists()
    assert not list((data_root / "voice").glob(".whisper-import-*"))


def test_voice_model_setup_rejects_symlink_source_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    real = source / "encoder-real.onnx"
    real.write_bytes(b"real")
    (source / "encoder.onnx").unlink()
    try:
        (source / "encoder.onnx").symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "rejected"
    assert "звичайним локальним файлом" in result.message
    assert not (data_root / "voice" / "whisper").exists()


def test_voice_model_setup_rejects_symlinked_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    alias = tmp_path / "source-alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(alias)})

    assert result.status == "rejected"
    assert not (data_root / "voice" / "whisper").exists()


def test_voice_model_setup_reparse_data_root_fails_closed_without_platform_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    ordinary = os.lstat(data_root)
    submit_calls = 0

    class _ReparseDirectoryEvidence:
        st_mode = ordinary.st_mode
        st_file_attributes = model_setup._REPARSE_POINT

    original_lstat = model_setup.os.lstat

    def lstat(path: object) -> object:
        if Path(path) == data_root:
            return _ReparseDirectoryEvidence()
        return original_lstat(path)  # type: ignore[arg-type]

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        nonlocal submit_calls
        submit_calls += 1
        coroutine.close()
        return Future()

    monkeypatch.setattr(model_setup.os, "lstat", lstat)
    setup = PackagedVoiceModelSetup(data_root, submit=submit)

    snapshot = setup.snapshot()
    installed = setup.install({"source_root": str(source)})
    started = setup.start({"source_root": str(source)})

    assert snapshot["status"] == "partial"
    assert snapshot["can_import"] is False
    assert installed.status == "rejected"
    assert started.status == "rejected"
    assert submit_calls == 0
    assert not (data_root / "voice").exists()


def test_voice_model_setup_rejects_indirected_data_root_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / "nika-data"
    try:
        data_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    submit_calls = 0

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        nonlocal submit_calls
        submit_calls += 1
        coroutine.close()
        return Future()

    setup = PackagedVoiceModelSetup(data_root, submit=submit)
    snapshot = setup.snapshot()
    installed = setup.install({"source_root": str(source)})
    started = setup.start({"source_root": str(source)})

    assert snapshot["status"] == "partial"
    assert snapshot["can_import"] is False
    assert "папка даних" in str(snapshot["message"])
    assert installed.status == "rejected"
    assert started.status == "rejected"
    assert "папка даних" in installed.message
    assert "папка даних" in started.message
    assert submit_calls == 0
    assert not (outside / "voice").exists()


def test_voice_model_setup_rejects_indirected_canonical_install(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    target = outside / "whisper"
    target.mkdir(parents=True)
    for name in ("encoder.onnx", "decoder.onnx", "tokens.txt"):
        (target / name).write_bytes(b"installed")

    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    try:
        (data_root / "voice").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    setup = PackagedVoiceModelSetup(data_root)
    snapshot = setup.snapshot()

    assert snapshot["status"] == "partial"
    assert snapshot["installed"] is False
    assert snapshot["can_import"] is False


def test_voice_model_setup_rejects_oversized_preexisting_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "nika-data"
    target = data_root / "voice" / "whisper"
    target.mkdir(parents=True)
    (target / "encoder.onnx").write_bytes(b"12345")
    (target / "decoder.onnx").write_bytes(b"ok")
    (target / "tokens.txt").write_bytes(b"ok")
    monkeypatch.setattr(model_setup, "_ONNX_FILE_LIMIT", 4)

    snapshot = PackagedVoiceModelSetup(data_root).snapshot()

    assert snapshot["status"] == "partial"
    assert snapshot["installed"] is False
    assert snapshot["can_import"] is False


def test_voice_model_setup_requires_all_fixed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    (source / "decoder.onnx").unlink()
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "rejected"
    assert "decoder.onnx" in result.message
    assert not (data_root / "voice" / "whisper").exists()


def test_voice_model_setup_preserves_target_that_appears_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    target = data_root / "voice" / "whisper"
    original_copy = model_setup._copy_verified_regular_file
    calls = 0

    def create_competing_target(
        source_file: Path,
        destination: Path,
        expected: os.stat_result,
        *,
        cancel_event: model_setup.Event | None,
    ) -> None:
        nonlocal calls
        calls += 1
        original_copy(
            source_file,
            destination,
            expected,
            cancel_event=cancel_event,
        )
        if calls == 3:
            target.mkdir()
            (target / "owner.txt").write_text("other-process", encoding="utf-8")

    monkeypatch.setattr(
        model_setup,
        "_copy_verified_regular_file",
        create_competing_target,
    )
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "failed"
    assert (target / "owner.txt").read_text(encoding="utf-8") == "other-process"
    assert not list((data_root / "voice").glob(".whisper-import-*"))


def test_voice_model_setup_cleans_stage_after_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    data_root.mkdir()
    calls = 0

    def fail_second_copy(
        source_file: Path,
        destination: Path,
        expected: os.stat_result,
        *,
        cancel_event: model_setup.Event | None,
    ) -> None:
        del cancel_event
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("PRIVATE SOURCE DIAGNOSTIC")
        destination.write_bytes(source_file.read_bytes())
        assert expected.st_size == destination.stat().st_size

    monkeypatch.setattr(model_setup, "_copy_verified_regular_file", fail_second_copy)
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "failed"
    assert "PRIVATE" not in result.message
    assert not (data_root / "voice" / "whisper").exists()
    assert not list((data_root / "voice").glob(".whisper-import-*"))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"source_root": "/tmp", "extra": True},
        {"source_root": 7},
    ],
)
def test_voice_model_setup_rejects_noncanonical_payload(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    setup = PackagedVoiceModelSetup(tmp_path)

    with pytest.raises((TypeError, ValueError)):
        setup.install(payload)  # type: ignore[arg-type]


def test_voice_model_copy_rejects_partial_destination_write() -> None:
    class _PartialWriter:
        def write(self, data: bytes) -> int:
            return max(0, len(data) - 1)

    with pytest.raises(RuntimeError, match="partial write"):
        model_setup._stream_copy(  # noqa: SLF001 - focused copy fence regression
            io.BytesIO(b"model-bytes"),
            _PartialWriter(),  # type: ignore[arg-type]
            len(b"model-bytes"),
            cancel_event=None,
        )


def test_voice_model_copy_rejects_post_open_metadata_change(tmp_path: Path) -> None:
    source = tmp_path / "encoder.onnx"
    source.write_bytes(b"stable-model")
    expected = os.lstat(source)
    changed_mtime = expected.st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(expected.st_atime_ns, changed_mtime))

    with source.open("rb", buffering=0) as reader:
        with pytest.raises(RuntimeError, match="identity changed"):
            model_setup._require_same_regular_file(  # noqa: SLF001
                expected,
                os.fstat(reader.fileno()),
            )


@pytest.mark.skipif(os.name != "nt", reason="UNC path semantics are Windows-specific")
def test_voice_model_setup_rejects_unc_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    setup = PackagedVoiceModelSetup(tmp_path)

    result = setup.install({"source_root": r"\\server\share\nika-whisper"})

    assert result.status == "rejected"
    assert "UNC" in result.message


def test_voice_model_setup_rejects_hostile_string_subclass(tmp_path: Path) -> None:
    class _HostileString(str):
        def strip(self, chars: str | None = None) -> str:
            del chars
            raise AssertionError("behavioral string methods must not execute")

    setup = PackagedVoiceModelSetup(tmp_path)

    with pytest.raises(TypeError):
        setup.install({"source_root": _HostileString(str(tmp_path))})



def test_voice_model_import_action_and_ui_preserve_single_live_region() -> None:
    actions = build_default_action_registry()
    action = actions.get("voice.model.import")
    assert action.label == "Імпортувати голосову модель"
    assert action.default_binding is None

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    script = (root / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")

    assert 'id="voice-model-source"' in html
    assert 'data-action-id="voice.model.import"' in html
    assert '<p id="voice-model-status">' in html
    assert html.count('role="status"') == 1
    assert html.count('aria-live="polite"') == 1
    assert "function renderVoiceModelSetup(snapshot)" in script
    assert "state.voice_model_setup ?? null" in script
    assert (
        'payload.source_root = voiceModelSource?.value ?? ""'
        in script
    )
    assert 'voiceModelSource.value = "";' in script


def test_packaged_bridge_exposes_voice_model_setup_state_and_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeVoice:
        available = False

        def start(self, payload: dict[str, object]) -> UIResult:
            assert payload == {}
            return UIResult(
                request_id="voice",
                status="rejected",
                message="voice unavailable",
            )

        def cancel(self, payload: dict[str, object]) -> UIResult:
            assert payload == {}
            return UIResult(
                request_id="voice",
                status="completed",
                message="no active voice",
            )

        def snapshot(self) -> dict[str, object]:
            return {
                "schema": "nika.packaged-voice-state:v1",
                "available": False,
                "message": "voice unavailable",
                "turn": None,
            }

        def close(self) -> None:
            return None

    class _FakeSetup:
        def __init__(self, root: Path, *, submit: object = None) -> None:
            self.root = root
            self.submit = submit
            self.payloads: list[dict[str, object]] = []
            self.cancelled = 0
            self.closed = False

        def snapshot(self) -> dict[str, object]:
            return {
                "schema": "nika.packaged-voice-model-setup:v1",
                "status": "missing",
                "generation": 0,
                "active": False,
                "installed": False,
                "can_import": True,
                "restart_required": False,
                "message": "setup ready",
            }

        def start(self, payload: dict[str, object]) -> UIResult:
            self.payloads.append(payload)
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="model imported",
                focus_id="voice-heading",
            )

        def cancel(self, payload: dict[str, object]) -> UIResult:
            assert payload == {}
            self.cancelled += 1
            return UIResult(
                request_id="desktop-handler",
                status="completed",
                message="no active import",
                focus_id="voice-model-source",
            )

        def close(self) -> None:
            self.closed = True

    fake_voice = _FakeVoice()
    setups: list[_FakeSetup] = []

    def build_setup(root: Path, *, submit: object = None) -> _FakeSetup:
        setup = _FakeSetup(root, submit=submit)
        setups.append(setup)
        return setup

    monkeypatch.setattr(
        nika_windows,
        "build_packaged_voice",
        lambda _root, **_kwargs: fake_voice,
    )
    monkeypatch.setattr(nika_windows, "PackagedVoiceModelSetup", build_setup)

    config = AppConfig(database_path=(tmp_path / "nika.db").resolve())
    cleanup_callbacks: list[object] = []
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
        register_cleanup=cleanup_callbacks.append,
    )
    try:
        response = bridge.get_state()
        assert response["ok"] is True
        state = response["state"]
        assert state["voice_model_setup"] == setups[0].snapshot()
        assert setups[0].root == config.database_path.parent
        assert callable(setups[0].submit)

        result = bridge.dispatch(
            {
                "request_id": "voice-model-import",
                "action_id": "voice.model.import",
                "payload": {"source_root": r"C:\\models\\nika-whisper"},
            }
        )
        assert result["status"] == "completed"
        assert setups[0].payloads == [
            {"source_root": r"C:\\models\\nika-whisper"}
        ]

        cancelled = bridge.dispatch(
            {
                "request_id": "voice-model-cancel",
                "action_id": "voice.model.cancel",
                "payload": {},
            }
        )
        assert cancelled["status"] == "completed"
        assert setups[0].cancelled == 1
    finally:
        for cleanup in reversed(cleanup_callbacks):
            assert callable(cleanup)
            cleanup()
    assert setups[0].closed is True
