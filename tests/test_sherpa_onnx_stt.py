from __future__ import annotations

import asyncio
import io
import threading
import wave
from typing import ClassVar

import pytest

from nika_core.model_gateway.contracts import PrivacyClass
from nika_core.sherpa_onnx_stt import SherpaOnnxWhisperSpeechToTextAdapter
from nika_core.speech_to_text import (
    SpeechAudio,
    SpeechAudioFormat,
    SpeechToTextAdapterError,
    SpeechToTextFailureCode,
    SpeechToTextPolicy,
    SpeechToTextRequest,
)


class _FakeSamples:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    @property
    def size(self) -> int:
        return len(self.values)

    def astype(self, dtype):
        del dtype
        return self

    def __imul__(self, factor: float):
        self.values = [value * factor for value in self.values]
        return self


class _FakeNumpy:
    float32 = object()

    @staticmethod
    def frombuffer(payload: bytes, *, dtype: str) -> _FakeSamples:
        assert dtype == "<i2"
        values = [
            float(int.from_bytes(payload[index : index + 2], "little", signed=True))
            for index in range(0, len(payload), 2)
        ]
        return _FakeSamples(values)


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class _Stream:
    def __init__(self, text: str) -> None:
        self.result = _Result(text)
        self.accepted: list[tuple[int, list[float]]] = []

    def accept_waveform(self, sample_rate_hz: int, samples: _FakeSamples) -> None:
        self.accepted.append((sample_rate_hz, list(samples.values)))


class _Recognizer:
    def __init__(self, *, text: str = "Привіт", fail: Exception | None = None) -> None:
        self.text = text
        self.fail = fail
        self.create_calls = 0
        self.decode_calls = 0
        self.streams: list[_Stream] = []

    def create_stream(self) -> _Stream:
        self.create_calls += 1
        stream = _Stream(self.text)
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: _Stream) -> None:
        self.decode_calls += 1
        if self.fail is not None:
            raise self.fail
        assert stream in self.streams


class _BlockingRecognizer(_Recognizer):
    def __init__(self) -> None:
        super().__init__(text="Готово")
        self.started = threading.Event()
        self.release = threading.Event()

    def decode_stream(self, stream: _Stream) -> None:
        self.decode_calls += 1
        assert stream in self.streams
        self.started.set()
        self.release.wait(timeout=2.0)


def _request(
    *,
    provider_id: str = "sherpa-onnx-whisper",
    model: str = "whisper-uk-v1",
    language: str | None = "uk",
    audio: SpeechAudio | None = None,
) -> SpeechToTextRequest:
    if audio is None:
        audio = SpeechAudio(
            data=b"\x00\x00\x00@\x00\xc0",
            audio_format=SpeechAudioFormat.PCM_S16LE,
            sample_rate_hz=16_000,
            channels=1,
        )
    return SpeechToTextRequest(
        request_id="stt-1",
        provider_id=provider_id,
        model=model,
        audio=audio,
        language=language,
        privacy=PrivacyClass.PRIVATE,
        policy=SpeechToTextPolicy(timeout_seconds=1.0),
    )


@pytest.fixture
def fake_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nika_core.sherpa_onnx_stt._load_numpy", lambda: _FakeNumpy())


def test_pcm16_decode_returns_bound_route_and_normalized_samples(fake_numpy: None) -> None:
    recognizer = _Recognizer()
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    response = asyncio.run(adapter.transcribe(_request()))

    assert response.request_id == "stt-1"
    assert response.provider_id == "sherpa-onnx-whisper"
    assert response.model == "whisper-uk-v1"
    assert response.text == "Привіт"
    assert response.detected_language is None
    assert response.latency_ms is not None and response.latency_ms >= 0
    assert recognizer.decode_calls == 1
    rate, samples = recognizer.streams[0].accepted[0]
    assert rate == 16_000
    assert samples == [0.0, 0.5, -0.5]


def test_pcm16_odd_byte_count_fails_before_recognizer_decode(fake_numpy: None) -> None:
    recognizer = _Recognizer()
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )
    audio = SpeechAudio(
        data=b"\x00",
        audio_format=SpeechAudioFormat.PCM_S16LE,
        sample_rate_hz=16_000,
        channels=1,
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(adapter.transcribe(_request(audio=audio)))

    assert caught.value.code is SpeechToTextFailureCode.INVALID_AUDIO
    assert recognizer.create_calls == 0


def test_wav_pcm16_metadata_is_validated_and_decoded(fake_numpy: None) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00\x00@")
    audio = SpeechAudio(
        data=buffer.getvalue(),
        audio_format=SpeechAudioFormat.WAV,
        sample_rate_hz=16_000,
        channels=1,
    )
    recognizer = _Recognizer(text="Тест")
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    response = asyncio.run(adapter.transcribe(_request(audio=audio)))

    assert response.text == "Тест"
    rate, samples = recognizer.streams[0].accepted[0]
    assert rate == 16_000
    assert samples == [0.0, 0.5]


def test_wav_request_metadata_mismatch_fails_closed(fake_numpy: None) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\x00\x00")
    audio = SpeechAudio(
        data=buffer.getvalue(),
        audio_format=SpeechAudioFormat.WAV,
        sample_rate_hz=16_000,
        channels=1,
    )
    recognizer = _Recognizer()
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(adapter.transcribe(_request(audio=audio)))

    assert caught.value.code is SpeechToTextFailureCode.INVALID_AUDIO
    assert recognizer.create_calls == 0


@pytest.mark.parametrize(
    ("provider_id", "model", "language"),
    [
        ("other-provider", "whisper-uk-v1", "uk"),
        ("sherpa-onnx-whisper", "other-model", "uk"),
        ("sherpa-onnx-whisper", "whisper-uk-v1", "en"),
    ],
)
def test_route_and_language_mismatch_fail_before_native_effect(
    provider_id: str,
    model: str,
    language: str,
) -> None:
    recognizer = _Recognizer()
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(
            adapter.transcribe(
                _request(provider_id=provider_id, model=model, language=language)
            )
        )

    assert caught.value.code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert recognizer.create_calls == 0


def test_stereo_audio_fails_before_native_effect() -> None:
    recognizer = _Recognizer()
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )
    audio = SpeechAudio(
        data=b"\x00\x00\x00\x00",
        audio_format=SpeechAudioFormat.PCM_S16LE,
        sample_rate_hz=16_000,
        channels=2,
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(adapter.transcribe(_request(audio=audio)))

    assert caught.value.code is SpeechToTextFailureCode.INVALID_AUDIO
    assert recognizer.create_calls == 0


def test_native_decode_error_is_sanitized(fake_numpy: None) -> None:
    canary = r"C:\private\SECRET-MODEL.onnx?token=do-not-leak"
    recognizer = _Recognizer(fail=RuntimeError(canary))
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(adapter.transcribe(_request()))

    assert caught.value.code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert str(caught.value) == "Sherpa ONNX STT native decode failed."
    assert caught.value.__suppress_context__ is True
    assert canary not in str(caught.value)
    assert canary not in repr(caught.value)


def test_cancelled_decode_remains_inflight_and_blocks_overlap(fake_numpy: None) -> None:
    async def scenario() -> None:
        recognizer = _BlockingRecognizer()
        adapter = SherpaOnnxWhisperSpeechToTextAdapter(
            recognizer=recognizer,
            model_id="whisper-uk-v1",
            language="uk",
        )
        first = asyncio.create_task(adapter.transcribe(_request()))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.5
        while not recognizer.started.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.005)
        assert recognizer.started.is_set()

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        with pytest.raises(SpeechToTextAdapterError) as busy:
            await adapter.transcribe(_request())
        assert busy.value.code is SpeechToTextFailureCode.RESOURCE_LIMIT
        assert busy.value.retryable is True
        assert recognizer.create_calls == 1

        recognizer.release.set()
        deadline = loop.time() + 0.5
        while adapter._inflight is not None and loop.time() < deadline:
            await asyncio.sleep(0.005)
        assert adapter._inflight is None

        response = await adapter.transcribe(_request())
        assert response.text == "Готово"
        assert recognizer.create_calls == 2

    asyncio.run(scenario())


def test_factory_binds_whisper_files_language_threads_and_cpu_provider() -> None:
    class _OfflineRecognizer:
        calls: ClassVar[list[dict[str, object]]] = []

        @classmethod
        def from_whisper(cls, **kwargs):
            cls.calls.append(dict(kwargs))
            return _Recognizer()

    class _SherpaModule:
        OfflineRecognizer = _OfflineRecognizer

    adapter = SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
        encoder=r"C:\models\encoder.onnx",
        decoder=r"C:\models\decoder.onnx",
        tokens=r"C:\models\tokens.txt",
        model_id="whisper-uk-v1",
        language="uk",
        num_threads=3,
        sherpa_module=_SherpaModule,
    )

    assert adapter.provider_id == "sherpa-onnx-whisper"
    assert adapter.supported_models == ("whisper-uk-v1",)
    assert _OfflineRecognizer.calls == [
        {
            "encoder": r"C:\models\encoder.onnx",
            "decoder": r"C:\models\decoder.onnx",
            "tokens": r"C:\models\tokens.txt",
            "language": "uk",
            "task": "transcribe",
            "num_threads": 3,
            "debug": False,
            "provider": "cpu",
        }
    ]


@pytest.mark.parametrize(
    "bad_path",
    [
        "https://example.com/model.onnx",
        "file://server/share/model.onnx",
        r"\\server\share\model.onnx",
    ],
)
def test_factory_rejects_nonlocal_model_paths_before_module_use(bad_path: str) -> None:
    class _OfflineRecognizer:
        @classmethod
        def from_whisper(cls, **kwargs):
            raise AssertionError(f"native factory must not run: {kwargs}")

    class _SherpaModule:
        OfflineRecognizer = _OfflineRecognizer

    with pytest.raises(ValueError, match="bounded local path"):
        SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
            encoder=bad_path,
            decoder="decoder.onnx",
            tokens="tokens.txt",
            model_id="whisper-uk-v1",
            language="uk",
            sherpa_module=_SherpaModule,
        )


@pytest.mark.parametrize(
    "bad_text",
    [
        "bad\x00transcript",
        "bad" + chr(0xD800) + "transcript",
    ],
)
def test_native_transcript_invalid_unicode_is_rejected(
    fake_numpy: None,
    bad_text: str,
) -> None:
    recognizer = _Recognizer(text=bad_text)
    adapter = SherpaOnnxWhisperSpeechToTextAdapter(
        recognizer=recognizer,
        model_id="whisper-uk-v1",
        language="uk",
    )

    with pytest.raises(SpeechToTextAdapterError) as caught:
        asyncio.run(adapter.transcribe(_request()))

    assert caught.value.code is SpeechToTextFailureCode.PROVIDER_ERROR
    assert str(caught.value) == "Sherpa ONNX STT result text is invalid."

def test_factory_native_initialization_error_is_sanitized() -> None:
    canary = r"C:\private\SECRET-DECODER.onnx"

    class _OfflineRecognizer:
        @classmethod
        def from_whisper(cls, **kwargs):
            del kwargs
            raise OSError(canary)

    class _SherpaModule:
        OfflineRecognizer = _OfflineRecognizer

    with pytest.raises(SpeechToTextAdapterError) as caught:
        SherpaOnnxWhisperSpeechToTextAdapter.from_whisper_files(
            encoder="encoder.onnx",
            decoder="decoder.onnx",
            tokens="tokens.txt",
            model_id="whisper-uk-v1",
            language="uk",
            sherpa_module=_SherpaModule,
        )

    assert caught.value.code is SpeechToTextFailureCode.UNAVAILABLE
    assert str(caught.value) == "Sherpa ONNX Whisper model initialization failed."
    assert caught.value.__suppress_context__ is True
    assert canary not in str(caught.value)
