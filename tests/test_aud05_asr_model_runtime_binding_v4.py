from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.model_evidence import inspect_model_directory
from nika_core.media.transcribers import FasterWhisperTranscriber


def test_checksum_approved_runtime_uses_verified_bytes_after_source_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "approved-model"
    model_root.mkdir()
    payload = model_root / "model.bin"
    trusted_bytes = b"A" * 64
    substituted_bytes = b"B" * 64
    payload.write_bytes(trusted_bytes)

    approved = inspect_model_directory(model_root, compute_sha256=True)
    assert approved.sha256 is not None

    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.metadata.version",
        lambda package: "1.2.3" if package == "faster-whisper" else "unexpected",
    )

    observed: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(
            self,
            model_path: str,
            *,
            device: str,
            compute_type: str,
            local_files_only: bool,
        ) -> None:
            del device, compute_type
            observed["path"] = Path(model_path)
            observed["bytes"] = (Path(model_path) / "model.bin").read_bytes()
            observed["local_only"] = local_files_only

    def mutate_source_then_import(package: str) -> object:
        assert package == "faster_whisper"
        payload.write_bytes(substituted_bytes)
        return SimpleNamespace(WhisperModel=FakeWhisperModel)

    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.import_module",
        mutate_source_then_import,
    )

    descriptor = ModelDescriptor(
        model_id="approved-model",
        engine_id="faster-whisper",
        version="fixture-v1",
        license_reference="model-license://approved-model",
        sha256=approved.sha256,
        size_bytes=approved.size_bytes,
    )

    try:
        transcriber = FasterWhisperTranscriber(model_path=model_root, model=descriptor)
    except MediaError as exc:
        assert exc.code is MediaErrorCode.CHECKSUM_MISMATCH
        return

    assert transcriber.model.sha256 == approved.sha256
    assert observed["local_only"] is True
    assert observed["bytes"] == trusted_bytes
    assert observed["path"] != model_root.resolve()
    assert payload.read_bytes() == substituted_bytes
