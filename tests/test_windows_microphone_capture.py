from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib
import sys

import pytest

from nika_core.microphone_capture import (
    MicrophoneCaptureAdapterError,
    MicrophoneCaptureFailureCode,
    MicrophoneCapturePolicy,
    MicrophoneCaptureRequest,
    MicrophoneCaptureService,
    MicrophoneCaptureStatus,
)
from nika_core.windows_microphone_capture import WindowsWasapiMicrophoneCaptureAdapter


class _CallbackStop(Exception):
    pass


class _CallbackAbort(Exception):
    pass


class _FakeRawInputStream:
    def __init__(self, owner: _FakeSoundDevice, **kwargs) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self) -> None:
        self.started = True
        if self.owner.hold_open:
            return
        callback = self.kwargs["callback"]
        for raw, frames, status in self.owner.callback_blocks:
            try:
                callback(raw, frames, None, status)
            except (_CallbackStop, _CallbackAbort):
                break

    def abort(self, *, ignore_errors: bool = True) -> None:
        assert ignore_errors is True
        self.aborted = True

    def close(self, *, ignore_errors: bool = True) -> None:
        assert ignore_errors is True
        self.closed = True


class _FakeSoundDevice:
    CallbackStop = _CallbackStop
    CallbackAbort = _CallbackAbort

    def __init__(self) -> None:
        self.default_input_device = 7
        self.device_host_api = 1
        self.raw_device_name = "SECRET-RAW-MICROPHONE-NAME"
        self.callback_blocks = [
            (b"\x01\x00" * 3, 3, False),
            (b"\x02\x00" * 4, 4, False),
        ]
        self.hold_open = False
        self.fail_check = False
        self.replace_endpoint_during_check = False
        self.settings_calls: list[dict[str, object]] = []
        self.check_calls: list[dict[str, object]] = []
        self.streams: list[_FakeRawInputStream] = []

    def query_hostapis(self) -> tuple[dict[str, object], ...]:
        return (
            {"name": "Windows MME", "default_input_device": 1},
            {"name": "Windows WASAPI", "default_input_device": self.default_input_device},
        )

    def query_devices(self, device_index: int) -> dict[str, object]:
        return {
            "name": self.raw_device_name,
            "index": device_index,
            "hostapi": self.device_host_api,
            "max_input_channels": 2,
            "default_samplerate": 48_000.0,
        }

    def WasapiSettings(self, **kwargs):
        self.settings_calls.append(dict(kwargs))
        return ("wasapi-settings", dict(kwargs))

    def check_input_settings(self, **kwargs) -> None:
        self.check_calls.append(dict(kwargs))
        if self.replace_endpoint_during_check:
            self.raw_device_name = "REPLACEMENT-DURING-CAPABILITY-CHECK"
        if self.fail_check:
            raise RuntimeError(f"unsupported format on {self.raw_device_name}")

    def RawInputStream(self, **kwargs):
        stream = _FakeRawInputStream(self, **kwargs)
        self.streams.append(stream)
        return stream


def _adapter(sd: _FakeSoundDevice | None = None) -> WindowsWasapiMicrophoneCaptureAdapter:
    return WindowsWasapiMicrophoneCaptureAdapter(
        sounddevice_module=sd or _FakeSoundDevice(),
        platform_name="win32",
    )


def _request(
    adapter: WindowsWasapiMicrophoneCaptureAdapter,
    *,
    sample_count: int = 5,
) -> MicrophoneCaptureRequest:
    capabilities = adapter.capabilities
    return MicrophoneCaptureRequest(
        request_id="physical-capture-1",
        provider_id=capabilities.provider_id,
        device_id=capabilities.device_id,
        sample_rate_hz=16_000,
        sample_count=sample_count,
        policy=MicrophoneCapturePolicy(timeout_seconds=1.0),
    )


def _expected_device_id(sd: _FakeSoundDevice) -> str:
    material = f"{sd.device_host_api}\x00{sd.default_input_device}\x00{sd.raw_device_name}"
    return f"wasapi-device-sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def test_capabilities_use_only_wasapi_and_hide_raw_device_name() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)

    capabilities = adapter.capabilities

    assert capabilities.provider_id == "sounddevice-wasapi"
    assert capabilities.device_id == _expected_device_id(sd)
    assert capabilities.min_sample_rate_hz == 8_000
    assert capabilities.max_sample_rate_hz == 48_000
    assert sd.raw_device_name not in repr(capabilities)


def test_capture_returns_exact_pcm16_and_uses_wasapi_shared_conversion() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)

    response = asyncio.run(adapter.capture(request))

    assert response.request_id == request.request_id
    assert response.provider_id == "sounddevice-wasapi"
    assert response.device_id == request.device_id
    assert response.sample_rate_hz == 16_000
    assert response.pcm_s16le == b"\x01\x00" * 3 + b"\x02\x00" * 2
    assert response.latency_ms >= 0.0
    assert sd.settings_calls[-1] == {
        "exclusive": False,
        "auto_convert": True,
        "explicit_sample_format": False,
    }
    assert sd.check_calls[-1]["device"] == 7
    assert sd.check_calls[-1]["channels"] == 1
    assert sd.check_calls[-1]["dtype"] == "int16"
    assert sd.check_calls[-1]["samplerate"] == 16_000
    stream = sd.streams[-1]
    assert stream.kwargs["blocksize"] == 0
    assert stream.kwargs["latency"] == "high"
    assert stream.started is True
    assert stream.aborted is True
    assert stream.closed is True
    assert sd.raw_device_name not in repr(response)


def test_adapter_composes_through_canonical_microphone_service() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)

    result = asyncio.run(MicrophoneCaptureService(adapter).capture(request))

    assert result.evidence.status is MicrophoneCaptureStatus.SUCCEEDED
    assert result.evidence.error_code is None
    assert result.pcm_s16le == b"\x01\x00" * 3 + b"\x02\x00" * 2
    assert sd.raw_device_name not in repr(result.evidence.as_dict())


def test_native_sounddevice_import_failure_is_minimized(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = WindowsWasapiMicrophoneCaptureAdapter(
        sounddevice_module=None,
        platform_name="win32",
    )
    original_import = builtins.__import__
    canary = r"C:\\private\\SECRET-PORTAUDIO.dll?token=do-not-leak"

    def guarded_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise OSError(canary)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(MicrophoneCaptureAdapterError) as caught:
        _ = adapter.capabilities

    assert caught.value.code is MicrophoneCaptureFailureCode.UNAVAILABLE
    assert caught.value.retryable is False
    assert str(caught.value) == "Windows microphone capture dependency is unavailable."
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    assert canary not in str(caught.value)
    assert canary not in repr(caught.value)


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_native_import_does_not_swallow_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    signal: type[BaseException],
) -> None:
    adapter = WindowsWasapiMicrophoneCaptureAdapter(
        sounddevice_module=None,
        platform_name="win32",
    )
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise signal("control-flow-signal")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(signal, match="control-flow-signal"):
        _ = adapter.capabilities


def test_non_windows_platform_fails_closed_without_backend_use() -> None:
    sd = _FakeSoundDevice()
    adapter = WindowsWasapiMicrophoneCaptureAdapter(
        sounddevice_module=sd,
        platform_name="linux",
    )

    with pytest.raises(MicrophoneCaptureAdapterError, match="unavailable on this platform"):
        _ = adapter.capabilities

    assert sd.check_calls == []
    assert sd.streams == []


def test_noncanonical_host_api_container_fails_closed_without_effect() -> None:
    class _HostApiTuple(tuple):
        pass

    sd = _FakeSoundDevice()
    host_apis = sd.query_hostapis()

    def query_hostapis() -> tuple[dict[str, object], ...]:
        return _HostApiTuple(host_apis)

    sd.query_hostapis = query_hostapis
    adapter = _adapter(sd)

    with pytest.raises(MicrophoneCaptureAdapterError, match="invalid data"):
        _ = adapter.capabilities

    assert sd.check_calls == []
    assert sd.streams == []


def test_malformed_endpoint_unicode_is_sanitized_before_identity_escape() -> None:
    sd = _FakeSoundDevice()
    sd.raw_device_name = "PRIVATE-\ud800-ENDPOINT"
    adapter = _adapter(sd)

    with pytest.raises(MicrophoneCaptureAdapterError) as caught:
        _ = adapter.capabilities

    assert caught.value.code is MicrophoneCaptureFailureCode.UNAVAILABLE
    assert caught.value.retryable is False
    assert str(caught.value) == "Default WASAPI endpoint identity is invalid."
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    assert sd.check_calls == []
    assert sd.streams == []


def test_no_wasapi_host_never_falls_back_to_mme() -> None:
    sd = _FakeSoundDevice()

    def query_hostapis() -> tuple[dict[str, object], ...]:
        return ({"name": "Windows MME", "default_input_device": 1},)

    sd.query_hostapis = query_hostapis
    adapter = _adapter(sd)

    with pytest.raises(MicrophoneCaptureAdapterError, match="No default Windows WASAPI"):
        _ = adapter.capabilities

    assert sd.streams == []


def test_default_endpoint_drift_is_rejected_before_stream_start() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    sd.default_input_device = 8

    with pytest.raises(MicrophoneCaptureAdapterError, match="route changed"):
        asyncio.run(adapter.capture(request))

    assert sd.check_calls == []
    assert sd.streams == []


def test_same_index_endpoint_replacement_is_rejected_before_stream_start() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    sd.raw_device_name = "REPLACEMENT-MICROPHONE-SAME-INDEX"

    with pytest.raises(MicrophoneCaptureAdapterError, match="route changed"):
        asyncio.run(adapter.capture(request))

    assert sd.check_calls == []
    assert sd.streams == []


def test_endpoint_drift_during_capability_check_is_rejected_before_stream_start() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    sd.replace_endpoint_during_check = True

    with pytest.raises(MicrophoneCaptureAdapterError, match="before stream activation"):
        asyncio.run(adapter.capture(request))

    assert len(sd.check_calls) == 1
    assert sd.streams == []


def test_mutated_oversized_request_fails_before_allocation_or_stream_effect() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    object.__setattr__(request, "sample_count", 16_000 * 31)

    with pytest.raises(TypeError, match="sample_count"):
        asyncio.run(adapter.capture(request))

    assert sd.check_calls == []
    assert sd.streams == []


def test_unsupported_input_format_is_minimized_and_has_no_stream_effect() -> None:
    sd = _FakeSoundDevice()
    sd.fail_check = True
    adapter = _adapter(sd)
    request = _request(adapter)

    with pytest.raises(MicrophoneCaptureAdapterError) as caught:
        asyncio.run(adapter.capture(request))

    assert sd.raw_device_name not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert sd.streams == []


def test_malformed_native_stream_cleanup_cannot_escape_sanitized_boundary() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    canary = r"C:\\private\\SECRET-STREAM-DIAGNOSTIC"

    class _MalformedNativeStream:
        def start(self) -> None:
            raise RuntimeError(canary)

        @property
        def abort(self):
            raise OSError(f"abort leaked {canary}")

    def raw_input_stream(**kwargs):
        del kwargs
        return _MalformedNativeStream()

    sd.RawInputStream = raw_input_stream

    with pytest.raises(MicrophoneCaptureAdapterError) as caught:
        asyncio.run(adapter.capture(request))

    assert caught.value.code is MicrophoneCaptureFailureCode.ADAPTER_ERROR
    assert str(caught.value) == "Windows microphone capture backend failed."
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    assert canary not in str(caught.value)
    assert canary not in repr(caught.value)


def test_callback_status_fails_closed_without_returning_partial_audio() -> None:
    sd = _FakeSoundDevice()
    sd.callback_blocks = [(b"\x01\x00" * 5, 5, True)]
    adapter = _adapter(sd)
    request = _request(adapter)

    with pytest.raises(MicrophoneCaptureAdapterError, match="invalid audio"):
        asyncio.run(adapter.capture(request))

    assert sd.streams[-1].aborted is True
    assert sd.streams[-1].closed is True


def test_short_callback_buffer_fails_closed() -> None:
    sd = _FakeSoundDevice()
    sd.callback_blocks = [(b"\x01\x00", 5, False)]
    adapter = _adapter(sd)
    request = _request(adapter)

    with pytest.raises(MicrophoneCaptureAdapterError, match="invalid audio"):
        asyncio.run(adapter.capture(request))

    assert sd.streams[-1].aborted is True
    assert sd.streams[-1].closed is True


def test_direct_adapter_snapshots_request_before_async_capture_effects() -> None:
    async def scenario() -> None:
        sd = _FakeSoundDevice()
        sd.hold_open = True
        adapter = _adapter(sd)
        request = _request(adapter)
        original_request_id = request.request_id
        original_device_id = request.device_id
        original_sample_count = request.sample_count

        task = asyncio.create_task(adapter.capture(request))
        await asyncio.sleep(0)
        stream = sd.streams[-1]
        assert stream.started is True

        object.__setattr__(request, "request_id", "mutated-request")
        object.__setattr__(request, "device_id", "mutated-device")
        object.__setattr__(request, "sample_count", 1)

        callback = stream.kwargs["callback"]
        with pytest.raises(_CallbackStop):
            callback(b"\x03\x00" * original_sample_count, original_sample_count, None, False)

        response = await task

        assert response.request_id == original_request_id
        assert response.device_id == original_device_id
        assert response.sample_rate_hz == 16_000
        assert response.pcm_s16le == b"\x03\x00" * original_sample_count
        assert stream.aborted is True
        assert stream.closed is True

    asyncio.run(scenario())


def test_direct_adapter_rejects_noncanonical_request_identity_before_backend_use() -> None:
    sd = _FakeSoundDevice()
    adapter = _adapter(sd)
    request = _request(adapter)
    object.__setattr__(request, "request_id", "bad\nrequest")

    with pytest.raises(ValueError, match="bounded machine token"):
        asyncio.run(adapter.capture(request))

    assert sd.check_calls == []
    assert sd.streams == []


def test_caller_cancellation_aborts_and_closes_physical_stream() -> None:
    async def scenario() -> None:
        sd = _FakeSoundDevice()
        sd.hold_open = True
        adapter = _adapter(sd)
        request = _request(adapter)
        task = asyncio.create_task(adapter.capture(request))
        await asyncio.sleep(0)
        assert sd.streams[-1].started is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert sd.streams[-1].aborted is True
        assert sd.streams[-1].closed is True

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="bundled PortAudio proof is Windows-only")
def test_installed_sounddevice_loads_portaudio_on_windows() -> None:
    sounddevice = importlib.import_module("sounddevice")

    version, version_text = sounddevice.get_portaudio_version()

    assert type(version) is int and version > 0
    assert type(version_text) is str and version_text.strip()
    assert callable(sounddevice.RawInputStream)
    assert callable(sounddevice.WasapiSettings)
