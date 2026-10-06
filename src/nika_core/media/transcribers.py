from __future__ import annotations

import importlib
import importlib.metadata
import time
import wave
from pathlib import Path

from nika_core.media.contracts import EngineDescriptor, ModelDescriptor, Segment
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.transcription import TranscriptionRequest, TranscriptionResult


def _require_local_path(path: Path, *, label: str, directory: bool = False) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            f"{label} is missing; acquire/approve the model explicitly before transcription",
        ) from exc
    valid = resolved.is_dir() if directory else resolved.is_file()
    if not valid:
        expected = "directory" if directory else "file"
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            f"{label} must be an existing local {expected}",
        )
    return resolved


class FasterWhisperTranscriber:
    """Optional local-only faster-whisper adapter. Model acquisition stays external."""

    def __init__(
        self,
        *,
        model_path: Path,
        model: ModelDescriptor,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_path = _require_local_path(
            model_path,
            label="faster-whisper model directory",
            directory=True,
        )
        self._model_descriptor = model
        try:
            version = importlib.metadata.version("faster-whisper")
        except importlib.metadata.PackageNotFoundError as exc:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                "faster-whisper is not installed; approve/install it explicitly",
            ) from exc
        self._engine = EngineDescriptor(
            engine_id="faster-whisper",
            name="faster-whisper",
            version=version,
            license_id="MIT",
            source_reference="https://github.com/SYSTRAN/faster-whisper",
        )
        if model.engine_id != self._engine.engine_id:
            raise ValueError("model descriptor must belong to faster-whisper")
        module = importlib.import_module("faster_whisper")
        self._runtime = module.WhisperModel(
            str(self._model_path),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )

    @property
    def engine(self) -> EngineDescriptor:
        return self._engine

    @property
    def model(self) -> ModelDescriptor:
        return self._model_descriptor

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        audio = request.audio_path.resolve(strict=True)
        started = time.monotonic()
        raw_segments, info = self._runtime.transcribe(
            str(audio),
            language=request.language,
            task="transcribe",
            condition_on_previous_text=False,
            vad_filter=False,
            word_timestamps=False,
        )
        segments = tuple(
            Segment(
                segment_id=f"{request.chunk_id}:{index:06d}",
                start_ms=request.offset_ms + max(0, round(float(item.start) * 1000)),
                end_ms=request.offset_ms + max(0, round(float(item.end) * 1000)),
                text=str(item.text).strip(),
            )
            for index, item in enumerate(raw_segments)
            if str(item.text).strip()
        )
        return TranscriptionResult(
            chunk_id=request.chunk_id,
            language=request.language or getattr(info, "language", None),
            segments=segments,
            engine=self.engine,
            model=self.model,
            elapsed_seconds=time.monotonic() - started,
        )


class SherpaOnnxWhisperTranscriber:
    """Optional local sherpa-onnx Whisper adapter using explicit local model files."""

    def __init__(
        self,
        *,
        encoder: Path,
        decoder: Path,
        tokens: Path,
        model: ModelDescriptor,
        language: str = "auto",
        num_threads: int = 2,
    ) -> None:
        paths = (
            _require_local_path(encoder, label="sherpa-onnx encoder"),
            _require_local_path(decoder, label="sherpa-onnx decoder"),
            _require_local_path(tokens, label="sherpa-onnx tokens"),
        )
        try:
            version = importlib.metadata.version("sherpa-onnx")
        except importlib.metadata.PackageNotFoundError as exc:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                "sherpa-onnx is not installed; approve/install it explicitly",
            ) from exc
        self._engine = EngineDescriptor(
            engine_id="sherpa-onnx",
            name="sherpa-onnx",
            version=version,
            license_id="Apache-2.0",
            source_reference="https://github.com/k2-fsa/sherpa-onnx",
        )
        if model.engine_id != self._engine.engine_id:
            raise ValueError("model descriptor must belong to sherpa-onnx")
        self._model_descriptor = model
        module = importlib.import_module("sherpa_onnx")
        self._recognizer = module.OfflineRecognizer.from_whisper(
            encoder=str(paths[0]),
            decoder=str(paths[1]),
            tokens=str(paths[2]),
            language=language,
            task="transcribe",
            num_threads=num_threads,
        )

    @property
    def engine(self) -> EngineDescriptor:
        return self._engine

    @property
    def model(self) -> ModelDescriptor:
        return self._model_descriptor

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        try:
            import numpy as np
        except ImportError as exc:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                "numpy is required by the sherpa-onnx adapter",
            ) from exc
        audio = request.audio_path.resolve(strict=True)
        started = time.monotonic()
        with wave.open(str(audio), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise ValueError("sherpa-onnx adapter requires normalized mono PCM16 WAV")
            sample_rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        text = str(getattr(stream.result, "text", "")).strip()
        duration_ms = round(len(samples) / sample_rate * 1000) if sample_rate else 0
        segments = (
            Segment(
                segment_id=f"{request.chunk_id}:000000",
                start_ms=request.offset_ms,
                end_ms=request.offset_ms + duration_ms,
                text=text,
            ),
        ) if text else ()
        return TranscriptionResult(
            chunk_id=request.chunk_id,
            language=request.language,
            segments=segments,
            engine=self.engine,
            model=self.model,
            elapsed_seconds=time.monotonic() - started,
        )
