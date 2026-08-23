from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.model_evidence import inspect_model_directory
from nika_core.media.transcribers import FasterWhisperTranscriber


def test_verified_asr_bundle_cannot_change_before_runtime_consumes_it(
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

    class FakeWhisperModel:
        def __init__(
            self,
            model_path: str,
            *,
            device: str,
            compute_type: str,
            local_files_only: bool,
        ) -> None:
            del device, compute_type, local_files_only
            # Prove that the native-runtime boundary is now observing bytes that were
            # never covered by the approved digest. The replacement preserves file size,
            # so size-only revalidation would not close this integrity gap.
            assert (Path(model_path) / "model.bin").read_bytes() == substituted_bytes

    def mutate_then_import(package: str) -> object:
        assert package == "faster_whisper"
        # Deterministic effect between DEV25's checksum preflight and the runtime's path
        # consumption. This models another local process replacing a writable model file.
        payload.write_bytes(substituted_bytes)
        return SimpleNamespace(WhisperModel=FakeWhisperModel)

    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.import_module",
        mutate_then_import,
    )

    descriptor = ModelDescriptor(
        model_id="approved-model",
        engine_id="faster-whisper",
        version="fixture-v1",
        license_reference="model-license://approved-model",
        sha256=approved.sha256,
        size_bytes=approved.size_bytes,
    )

    with pytest.raises(MediaError) as caught:
        FasterWhisperTranscriber(model_path=model_root, model=descriptor)

    assert caught.value.code is MediaErrorCode.CHECKSUM_MISMATCH
