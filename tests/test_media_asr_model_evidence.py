from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.model_evidence import (
    LocalModelEvidence,
    bind_model_evidence,
    inspect_model_directory,
    inspect_model_files,
)
from nika_core.media.transcribers import (
    FasterWhisperTranscriber,
    SherpaOnnxWhisperTranscriber,
)


class FakeWhisperModel:
    calls: list[dict[str, object]] = []

    def __init__(
        self,
        model_path: str,
        *,
        device: str,
        compute_type: str,
        local_files_only: bool,
    ) -> None:
        self.calls.append(
            {
                "model_path": model_path,
                "device": device,
                "compute_type": compute_type,
                "local_files_only": local_files_only,
            }
        )


class FakeOfflineRecognizer:
    calls: list[dict[str, object]] = []

    @classmethod
    def from_whisper(cls, **kwargs):
        cls.calls.append(dict(kwargs))
        return cls()


def _patch_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWhisperModel.calls.clear()
    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.metadata.version",
        lambda package: "1.2.3" if package == "faster-whisper" else "unexpected",
    )
    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.import_module",
        lambda package: SimpleNamespace(WhisperModel=FakeWhisperModel)
        if package == "faster_whisper"
        else pytest.fail(f"unexpected import: {package}"),
    )


def _patch_sherpa(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeOfflineRecognizer.calls.clear()
    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.metadata.version",
        lambda package: "1.2.3" if package == "sherpa-onnx" else "unexpected",
    )
    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.import_module",
        lambda package: SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_whisper=FakeOfflineRecognizer.from_whisper)
        )
        if package == "sherpa_onnx"
        else pytest.fail(f"unexpected import: {package}"),
    )


def test_faster_whisper_preflight_is_local_only_and_binds_observed_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "модель з пробілами"
    model_root.mkdir()
    (model_root / "config.json").write_text('{"model":"fixture"}', encoding="utf-8")
    (model_root / "model.bin").write_bytes(b"model-payload")
    _patch_faster_whisper(monkeypatch)

    adapter = FasterWhisperTranscriber(
        model_path=model_root,
        model=ModelDescriptor(
            model_id="fixture-model",
            engine_id="faster-whisper",
            version="fixture-v1",
            license_reference="model-license://fixture",
        ),
    )

    assert adapter.engine.version == "1.2.3"
    assert adapter.engine.license_id == "MIT"
    assert adapter.model.license_reference == "model-license://fixture"
    assert adapter.model.size_bytes == sum(path.stat().st_size for path in model_root.iterdir())
    assert adapter.model.sha256 is None
    assert FakeWhisperModel.calls == [
        {
            "model_path": str(model_root.resolve()),
            "device": "cpu",
            "compute_type": "int8",
            "local_files_only": True,
        }
    ]


def test_faster_whisper_rejects_checksum_mismatch_before_runtime_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    (model_root / "model.bin").write_bytes(b"model")
    imported = False

    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.metadata.version",
        lambda _package: "1.2.3",
    )

    def reject_runtime_import(_package: str):
        nonlocal imported
        imported = True
        raise AssertionError("runtime import must occur only after model identity verification")

    monkeypatch.setattr(
        "nika_core.media.transcribers.importlib.import_module",
        reject_runtime_import,
    )

    with pytest.raises(MediaError) as caught:
        FasterWhisperTranscriber(
            model_path=model_root,
            model=ModelDescriptor(
                model_id="fixture-model",
                engine_id="faster-whisper",
                version="fixture-v1",
                license_reference="model-license://fixture",
                sha256="0" * 64,
            ),
        )

    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert caught.value.retryable is False
    assert imported is False


def test_faster_whisper_accepts_exact_declared_bundle_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    (model_root / "config.json").write_bytes(b"config")
    (model_root / "model.bin").write_bytes(b"weights")
    evidence = inspect_model_directory(model_root, compute_sha256=True)
    assert evidence.sha256 is not None
    _patch_faster_whisper(monkeypatch)

    adapter = FasterWhisperTranscriber(
        model_path=model_root,
        model=ModelDescriptor(
            model_id="fixture-model",
            engine_id="faster-whisper",
            version="fixture-v1",
            license_reference="model-license://fixture",
            sha256=evidence.sha256,
            size_bytes=evidence.size_bytes,
        ),
    )

    assert adapter.model.sha256 == evidence.sha256
    assert adapter.model.size_bytes == evidence.size_bytes


def test_size_only_model_preflight_does_not_read_payload_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    payload = model_root / "model.bin"
    payload.write_bytes(b"x" * 1024)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("payload hashing must be opt-in through an approved sha256")

    monkeypatch.setattr(Path, "open", forbidden_open)
    evidence = inspect_model_directory(model_root, compute_sha256=False)

    assert evidence.sha256 is None
    assert evidence.size_bytes == 1024
    assert evidence.file_count == 1


def test_model_preflight_rejects_filesystem_indirection_portably(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    linked = model_root / "linked"
    linked.write_bytes(b"fixture")
    original = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path == linked:
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(MediaError) as caught:
        inspect_model_directory(model_root, compute_sha256=False)

    assert caught.value.code == MediaErrorCode.PATH_ESCAPE


def test_model_preflight_rejects_parent_indirection_portably(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    model_root = parent / "model"
    model_root.mkdir()
    (model_root / "model.bin").write_bytes(b"fixture")
    parent = parent.absolute()
    original = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path == parent:
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(MediaError) as caught:
        inspect_model_directory(model_root, compute_sha256=False)

    assert caught.value.code == MediaErrorCode.PATH_ESCAPE


def test_empty_model_directory_is_explicit_component_failure(tmp_path: Path) -> None:
    model_root = tmp_path / "empty"
    model_root.mkdir()

    with pytest.raises(MediaError) as caught:
        inspect_model_directory(model_root, compute_sha256=False)

    assert caught.value.code == MediaErrorCode.COMPONENT_MISSING
    assert caught.value.retryable is False


def test_sherpa_model_files_are_bound_by_roles_and_keep_license_separate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files = {
        "encoder": tmp_path / "encoder.onnx",
        "decoder": tmp_path / "decoder.onnx",
        "tokens": tmp_path / "tokens.txt",
    }
    files["encoder"].write_bytes(b"encoder")
    files["decoder"].write_bytes(b"decoder")
    files["tokens"].write_text("token", encoding="utf-8")
    evidence = inspect_model_files(files, compute_sha256=True)
    assert evidence.sha256 is not None
    _patch_sherpa(monkeypatch)

    adapter = SherpaOnnxWhisperTranscriber(
        encoder=files["encoder"],
        decoder=files["decoder"],
        tokens=files["tokens"],
        model=ModelDescriptor(
            model_id="sherpa-fixture",
            engine_id="sherpa-onnx",
            version="fixture-v1",
            license_reference="model-license://sherpa-fixture",
            sha256=evidence.sha256,
            size_bytes=evidence.size_bytes,
        ),
    )

    assert adapter.engine.license_id == "Apache-2.0"
    assert adapter.model.license_reference == "model-license://sherpa-fixture"
    assert adapter.model.sha256 == evidence.sha256
    assert FakeOfflineRecognizer.calls[0]["encoder"] == str(files["encoder"].resolve())
    assert FakeOfflineRecognizer.calls[0]["decoder"] == str(files["decoder"].resolve())
    assert FakeOfflineRecognizer.calls[0]["tokens"] == str(files["tokens"].resolve())


def test_model_size_mismatch_fails_closed() -> None:
    descriptor = ModelDescriptor(
        model_id="fixture",
        engine_id="faster-whisper",
        version="fixture-v1",
        license_reference="model-license://fixture",
        size_bytes=2,
    )

    with pytest.raises(MediaError) as caught:
        bind_model_evidence(
            descriptor,
            LocalModelEvidence(
                size_bytes=3,
                file_count=1,
                sha256=None,
            ),
        )

    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH


def test_model_license_reference_is_required_for_runtime_evidence() -> None:
    descriptor = ModelDescriptor(
        model_id="fixture",
        engine_id="faster-whisper",
        version="fixture-v1",
        license_reference=" ",
    )

    with pytest.raises(ValueError, match="license_reference"):
        bind_model_evidence(
            descriptor,
            LocalModelEvidence(
                size_bytes=1,
                file_count=1,
                sha256=None,
            ),
        )
