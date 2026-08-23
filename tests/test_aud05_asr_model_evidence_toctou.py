from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.media.transcribers as transcribers
from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError
from nika_core.media.model_evidence import inspect_model_directory


def test_approved_faster_whisper_hash_cannot_be_swapped_before_runtime_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "approved-model"
    model_dir.mkdir()
    model_file = model_dir / "model.bin"
    model_file.write_bytes(b"approved")

    approved = inspect_model_directory(model_dir, compute_sha256=True)
    assert approved.sha256 is not None
    descriptor = ModelDescriptor(
        model_id="approved-model",
        engine_id="faster-whisper",
        version="1",
        license_reference="test-license",
        sha256=approved.sha256,
        size_bytes=approved.size_bytes,
    )

    observed: dict[str, bytes] = {}

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
            assert local_files_only is True
            observed["runtime_bytes"] = (Path(path) / "model.bin").read_bytes()

    real_inspect = transcribers.inspect_model_directory

    def inspect_then_swap(root: Path, *, compute_sha256: bool):
        evidence = real_inspect(root, compute_sha256=compute_sha256)
        # Same-size replacement isolates the checksum/runtime binding defect.
        (root / "model.bin").write_bytes(b"attacker")
        return evidence

    monkeypatch.setattr(transcribers, "inspect_model_directory", inspect_then_swap)
    monkeypatch.setattr(transcribers.importlib.metadata, "version", lambda _name: "1.0")
    monkeypatch.setattr(
        transcribers.importlib,
        "import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    with pytest.raises(MediaError):
        transcribers.FasterWhisperTranscriber(
            model_path=model_dir,
            model=descriptor,
        )

    assert observed.get("runtime_bytes") != b"attacker"
