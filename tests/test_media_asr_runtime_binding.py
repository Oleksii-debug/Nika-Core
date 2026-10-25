from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media import transcribers
from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.model_evidence import inspect_model_directory, inspect_model_files


def _faster_descriptor(model_dir: Path) -> ModelDescriptor:
    evidence = inspect_model_directory(model_dir, compute_sha256=True)
    assert evidence.sha256 is not None
    return ModelDescriptor(
        model_id="approved-model",
        engine_id="faster-whisper",
        version="1",
        license_reference="test-license",
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
    )


def test_faster_whisper_rejects_same_size_swap_before_runtime_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "approved-model"
    model_dir.mkdir()
    model_file = model_dir / "model.bin"
    model_file.write_bytes(b"approved")
    descriptor = _faster_descriptor(model_dir)

    real_inspect = transcribers.inspect_model_directory

    def inspect_then_swap(root: Path, *, compute_sha256: bool):
        evidence = real_inspect(root, compute_sha256=compute_sha256)
        (root / "model.bin").write_bytes(b"attacker")
        return evidence

    runtime_imported = False

    def reject_runtime_import(_name: str):
        nonlocal runtime_imported
        runtime_imported = True
        raise AssertionError("runtime import must follow private snapshot verification")

    monkeypatch.setattr(transcribers, "inspect_model_directory", inspect_then_swap)
    monkeypatch.setattr(transcribers.importlib.metadata, "version", lambda _name: "1.0")
    monkeypatch.setattr(transcribers.importlib, "import_module", reject_runtime_import)

    with pytest.raises(MediaError) as caught:
        transcribers.FasterWhisperTranscriber(
            model_path=model_dir,
            model=descriptor,
        )

    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert runtime_imported is False


def test_sherpa_rejects_same_size_swap_before_runtime_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files: dict[str, Path] = {
        "encoder": tmp_path / "encoder.onnx",
        "decoder": tmp_path / "decoder.onnx",
        "tokens": tmp_path / "tokens.txt",
    }
    files["encoder"].write_bytes(b"approved")
    files["decoder"].write_bytes(b"approved")
    files["tokens"].write_bytes(b"approved")
    evidence = inspect_model_files(files, compute_sha256=True)
    assert evidence.sha256 is not None
    descriptor = ModelDescriptor(
        model_id="approved-sherpa",
        engine_id="sherpa-onnx",
        version="1",
        license_reference="test-license",
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
    )

    real_inspect = transcribers.inspect_model_files

    def inspect_then_swap(selected: dict[str, Path], *, compute_sha256: bool):
        current = real_inspect(selected, compute_sha256=compute_sha256)
        selected["decoder"].write_bytes(b"attacker")
        return current

    runtime_imported = False

    def reject_runtime_import(_name: str):
        nonlocal runtime_imported
        runtime_imported = True
        raise AssertionError("runtime import must follow private snapshot verification")

    monkeypatch.setattr(transcribers, "inspect_model_files", inspect_then_swap)
    monkeypatch.setattr(transcribers.importlib.metadata, "version", lambda _name: "1.0")
    monkeypatch.setattr(transcribers.importlib, "import_module", reject_runtime_import)

    with pytest.raises(MediaError) as caught:
        transcribers.SherpaOnnxWhisperTranscriber(
            encoder=files["encoder"],
            decoder=files["decoder"],
            tokens=files["tokens"],
            model=descriptor,
        )

    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert runtime_imported is False


def test_hash_approved_faster_whisper_runtime_uses_private_verified_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "approved-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_bytes(b"config")
    (model_dir / "model.bin").write_bytes(b"approved")
    descriptor = _faster_descriptor(model_dir)
    observed: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(
            self,
            path: str,
            *,
            device: str,
            compute_type: str,
            local_files_only: bool,
        ) -> None:
            del device, compute_type
            runtime_root = Path(path)
            observed["path"] = runtime_root
            observed["bytes"] = (runtime_root / "model.bin").read_bytes()
            observed["local_only"] = local_files_only

    monkeypatch.setattr(transcribers.importlib.metadata, "version", lambda _name: "1.0")
    monkeypatch.setattr(
        transcribers.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    adapter = transcribers.FasterWhisperTranscriber(
        model_path=model_dir,
        model=descriptor,
    )

    assert observed["path"] != model_dir.resolve()
    assert observed["bytes"] == b"approved"
    assert observed["local_only"] is True
    assert adapter.model.sha256 == descriptor.sha256
