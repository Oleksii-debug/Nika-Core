from __future__ import annotations

import asyncio
import hashlib
import sys
from typing import Any

from nika_core.microphone_capture import (
    MicrophoneCaptureAdapterError,
    MicrophoneCaptureCapabilities,
    MicrophoneCaptureFailureCode,
    MicrophoneCaptureRequest,
    MicrophoneCaptureResponse,
)

_PROVIDER_ID = "sounddevice-wasapi"
_WASAPI_HOST_API_NAME = "windows wasapi"
_MIN_SAMPLE_RATE_HZ = 8_000
_MAX_SAMPLE_RATE_HZ = 48_000
_MAX_CAPTURE_SECONDS = 30
_BYTES_PER_MONO_PCM16_FRAME = 2
_MAX_ENDPOINT_NAME_LENGTH = 1024


class WindowsWasapiMicrophoneCaptureAdapter:
    """Real Windows microphone adapter backed by sounddevice/PortAudio WASAPI.

    Raw Windows endpoint names remain inside this adapter and are reduced to a
    one-way logical identity before crossing the canonical microphone boundary.
    """

    def __init__(
        self,
        *,
        sounddevice_module: Any | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._sounddevice_module = sounddevice_module
        self._platform_name = sys.platform if platform_name is None else platform_name

    @property
    def capabilities(self) -> MicrophoneCaptureCapabilities:
        _, _, logical_device_id = self._resolve_wasapi_default_input()
        return MicrophoneCaptureCapabilities(
            provider_id=_PROVIDER_ID,
            device_id=logical_device_id,
            min_sample_rate_hz=_MIN_SAMPLE_RATE_HZ,
            max_sample_rate_hz=_MAX_SAMPLE_RATE_HZ,
        )

    async def capture(self, request: MicrophoneCaptureRequest) -> MicrophoneCaptureResponse:
        request = _snapshot_request(request)
        sd, device_index, logical_device_id = self._resolve_wasapi_default_input()
        if request.provider_id != _PROVIDER_ID or request.device_id != logical_device_id:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ROUTE_MISMATCH,
                "Windows microphone route changed before capture.",
                retryable=True,
            )

        extra_settings = self._wasapi_settings(sd)
        self._check_input_settings(
            sd,
            device_index=device_index,
            sample_rate_hz=request.sample_rate_hz,
            extra_settings=extra_settings,
        )
        current_sd, current_device_index, current_logical_device_id = (
            self._resolve_wasapi_default_input()
        )
        if (
            current_sd is not sd
            or current_device_index != device_index
            or current_logical_device_id != logical_device_id
        ):
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ROUTE_MISMATCH,
                "Windows microphone route changed before stream activation.",
                retryable=True,
            )

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        target_size = request.sample_count * _BYTES_PER_MONO_PCM16_FRAME
        pcm = bytearray(target_size)
        pcm_view = memoryview(pcm)
        completed: asyncio.Future[None] = loop.create_future()
        offset = 0
        callback_failed = False
        stream: Any | None = None

        def finish_once() -> None:
            if not completed.done():
                completed.set_result(None)

        def callback(indata: Any, frames: Any, _time_info: Any, status: Any) -> None:
            nonlocal callback_failed, offset
            if status:
                callback_failed = True
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackAbort
            if type(frames) is not int or frames <= 0:
                callback_failed = True
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackAbort
            try:
                data = memoryview(indata).cast("B")
            except (TypeError, ValueError):
                callback_failed = True
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackAbort
            expected_size = frames * _BYTES_PER_MONO_PCM16_FRAME
            if len(data) < expected_size:
                callback_failed = True
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackAbort
            remaining = target_size - offset
            if remaining <= 0:
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackStop
            copied = min(expected_size, remaining)
            pcm_view[offset : offset + copied] = data[:copied]
            offset += copied
            if offset >= target_size:
                loop.call_soon_threadsafe(finish_once)
                raise sd.CallbackStop

        try:
            stream = sd.RawInputStream(
                samplerate=request.sample_rate_hz,
                blocksize=0,
                device=device_index,
                channels=1,
                dtype="int16",
                latency="high",
                extra_settings=extra_settings,
                callback=callback,
            )
            stream.start()
            await completed
        except asyncio.CancelledError:
            _abort_and_close(stream)
            stream = None
            raise
        except MicrophoneCaptureAdapterError:
            _abort_and_close(stream)
            stream = None
            raise
        except Exception:  # noqa: BLE001 - isolate third-party native audio boundary
            _abort_and_close(stream)
            stream = None
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "Windows microphone capture backend failed.",
                retryable=True,
            ) from None
        finally:
            _abort_and_close(stream)

        if callback_failed or offset != target_size:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.ADAPTER_ERROR,
                "Windows microphone capture produced invalid audio.",
                retryable=True,
            )

        latency_ms = (loop.time() - started_at) * 1000.0
        return MicrophoneCaptureResponse(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            pcm_s16le=bytes(pcm),
            latency_ms=latency_ms,
        )

    def _resolve_wasapi_default_input(self) -> tuple[Any, int, str]:
        if self._platform_name != "win32":
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone capture is unavailable on this platform.",
                retryable=False,
            )
        sd = self._load_sounddevice()
        try:
            host_apis = sd.query_hostapis()
        except Exception:  # noqa: BLE001 - isolate third-party discovery boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone discovery failed.",
                retryable=True,
            ) from None
        if type(host_apis) is not tuple:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone discovery returned invalid data.",
                retryable=False,
            )

        host_api_index: int | None = None
        device_index: int | None = None
        for index, raw_host_api in enumerate(host_apis):
            if type(raw_host_api) is not dict:
                continue
            name = raw_host_api.get("name")
            if type(name) is not str or name.casefold() != _WASAPI_HOST_API_NAME:
                continue
            raw_default = raw_host_api.get("default_input_device")
            if type(raw_default) is not int or raw_default < 0:
                break
            host_api_index = index
            device_index = raw_default
            break

        if host_api_index is None or device_index is None:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "No default Windows WASAPI microphone is available.",
                retryable=True,
            )

        try:
            raw_device = sd.query_devices(device_index)
        except Exception:  # noqa: BLE001 - isolate third-party discovery boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone discovery failed.",
                retryable=True,
            ) from None
        if type(raw_device) is not dict:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone discovery returned invalid data.",
                retryable=False,
            )
        raw_index = raw_device.get("index")
        raw_host_api = raw_device.get("hostapi")
        raw_input_channels = raw_device.get("max_input_channels")
        raw_name = raw_device.get("name")
        if (
            type(raw_index) is not int
            or raw_index != device_index
            or type(raw_host_api) is not int
            or raw_host_api != host_api_index
            or type(raw_input_channels) is not int
            or raw_input_channels < 1
            or type(raw_name) is not str
            or not raw_name.strip()
            or len(raw_name) > _MAX_ENDPOINT_NAME_LENGTH
            or "\x00" in raw_name
        ):
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Default WASAPI endpoint is not a valid microphone input.",
                retryable=True,
            )
        try:
            logical_device_id = _logical_device_id(host_api_index, device_index, raw_name)
        except ValueError:
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Default WASAPI endpoint identity is invalid.",
                retryable=False,
            ) from None
        return sd, device_index, logical_device_id

    def _load_sounddevice(self) -> Any:
        if self._sounddevice_module is not None:
            return self._sounddevice_module
        try:
            import sounddevice  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - minimize Python/native dependency-load diagnostics
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows microphone capture dependency is unavailable.",
                retryable=False,
            ) from None
        self._sounddevice_module = sounddevice
        return sounddevice

    @staticmethod
    def _wasapi_settings(sd: Any) -> Any:
        try:
            return sd.WasapiSettings(
                exclusive=False,
                auto_convert=True,
                explicit_sample_format=False,
            )
        except Exception:  # noqa: BLE001 - isolate third-party settings boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.UNAVAILABLE,
                "Windows WASAPI microphone settings are unavailable.",
                retryable=False,
            ) from None

    @staticmethod
    def _check_input_settings(
        sd: Any,
        *,
        device_index: int,
        sample_rate_hz: int,
        extra_settings: Any,
    ) -> None:
        try:
            sd.check_input_settings(
                device=device_index,
                channels=1,
                dtype="int16",
                samplerate=sample_rate_hz,
                extra_settings=extra_settings,
            )
        except Exception:  # noqa: BLE001 - isolate third-party capability boundary
            raise MicrophoneCaptureAdapterError(
                MicrophoneCaptureFailureCode.RESOURCE_LIMIT,
                "Requested microphone format is not supported by the WASAPI endpoint.",
                retryable=False,
            ) from None


def _logical_device_id(host_api_index: int, device_index: int, raw_name: str) -> str:
    if (
        type(host_api_index) is not int
        or host_api_index < 0
        or type(device_index) is not int
        or device_index < 0
        or type(raw_name) is not str
        or not raw_name
    ):
        raise ValueError("WASAPI endpoint identity is invalid")
    try:
        material = f"{host_api_index}\x00{device_index}\x00{raw_name}".encode()
    except UnicodeEncodeError:
        raise ValueError("WASAPI endpoint identity is invalid") from None
    digest = hashlib.sha256(material).hexdigest()
    return f"wasapi-device-sha256:{digest}"


def _snapshot_request(request: MicrophoneCaptureRequest) -> MicrophoneCaptureRequest:
    if type(request) is not MicrophoneCaptureRequest:
        raise TypeError("request must be exact MicrophoneCaptureRequest")
    try:
        return MicrophoneCaptureRequest(
            request_id=request.request_id,
            provider_id=request.provider_id,
            device_id=request.device_id,
            sample_rate_hz=request.sample_rate_hz,
            sample_count=request.sample_count,
            policy=request.policy,
        )
    except AttributeError:
        raise TypeError("microphone capture request is incomplete") from None
    except ValueError as error:
        message = str(error)
        if message.startswith("sample_rate_hz "):
            raise TypeError("sample_rate_hz must be an exact supported integer") from None
        if message.startswith("sample_count "):
            raise TypeError("sample_count must be an exact supported positive integer") from None
        raise


def _abort_and_close(stream: Any | None) -> None:
    if stream is None:
        return
    _best_effort_cleanup(stream, "abort")
    _best_effort_cleanup(stream, "close")


def _best_effort_cleanup(stream: Any, operation_name: str) -> None:
    try:
        operation = getattr(stream, operation_name)
        if not callable(operation):
            return
        operation(ignore_errors=True)
    except Exception:  # noqa: BLE001 - teardown errors must not expose native diagnostics
        return
