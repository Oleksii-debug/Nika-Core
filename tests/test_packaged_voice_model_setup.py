from __future__ import annotations

import os
from pathlib import Path

import pytest

import nika_core.ui.packaged_voice_model_setup as model_setup
from nika_core.ui.packaged_voice_model_setup import PackagedVoiceModelSetup


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
    assert not (data_root / "voice" / ".whisper-import.lock").exists()

    restarted = PackagedVoiceModelSetup(data_root).snapshot()
    assert restarted["status"] == "installed"
    assert restarted["installed"] is True
    assert restarted["restart_required"] is False


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


def test_voice_model_setup_cleans_stage_but_preserves_foreign_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_setup.sys, "platform", "win32")
    source = _write_source(tmp_path)
    data_root = tmp_path / "nika-data"
    voice_root = data_root / "voice"
    voice_root.mkdir(parents=True)
    lock = voice_root / ".whisper-import.lock"
    lock.write_text("other-process", encoding="utf-8")
    setup = PackagedVoiceModelSetup(data_root)

    result = setup.install({"source_root": str(source)})

    assert result.status == "failed"
    assert lock.read_text(encoding="utf-8") == "other-process"
    assert not list(voice_root.glob(".whisper-import-*"))
    assert not (voice_root / "whisper").exists()


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
    ) -> None:
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
    assert not (data_root / "voice" / ".whisper-import.lock").exists()


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


def test_voice_model_setup_rejects_hostile_string_subclass(tmp_path: Path) -> None:
    class _HostileString(str):
        def strip(self, chars: str | None = None) -> str:
            del chars
            raise AssertionError("behavioral string methods must not execute")

    setup = PackagedVoiceModelSetup(tmp_path)

    with pytest.raises(TypeError):
        setup.install({"source_root": _HostileString(str(tmp_path))})
