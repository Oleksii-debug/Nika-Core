from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
    ModelRequest,
    ModelResourcePolicy,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceObserverPort


@dataclass(frozen=True, slots=True)
class FoundryModelEvidence:
    """Provider-neutral evidence extracted from Foundry's public model surface."""

    model_id: str
    model_version: str | None
    alias: str
    cached: bool
    loaded: bool
    path: str | None
    context_length: int | None
    input_modalities: str | None
    output_modalities: str | None
    capability_tags: str | None
    supports_tool_calling: bool | None


class FoundryLocalProvider:
    """Embedded Foundry Local provider using Microsoft's in-process Python SDK.

    Foundry Local is optimized for single-user on-device inference rather than
    server-style concurrent batching. Nika therefore serializes in-process
    completions per provider instance. The upstream non-streaming Python API
    does not expose a proven hard-cancel primitive, so a timed-out native
    inference keeps the provider slot until the worker actually exits.

    Model acquisition is a separate explicit product action. ``complete()``
    never downloads a model, even when a caller selects a different model in
    the request. This prevents ordinary inference from silently turning into a
    network/download operation.

    ``expected_model_id`` optionally pins the exact public Foundry variant ID.
    This lets release/physical-proof paths fail closed if a logical alias starts
    resolving to another artifact after a catalog or SDK change.
    """

    def __init__(
        self,
        *,
        default_model: str,
        app_name: str = "NikaCore",
        model_cache_dir: str | Path | None = None,
        allow_download: bool = False,
        expected_model_id: str | None = None,
        resource_policy: ModelResourcePolicy | None = None,
        resource_observer: ResourceObserverPort | None = None,
        manager_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not default_model.strip():
            raise ValueError("default_model must not be empty")
        if default_model != default_model.strip():
            raise ValueError("default_model must not contain surrounding whitespace")
        if allow_download:
            raise ValueError(
                "allow_download on FoundryLocalProvider is no longer supported; "
                "use download_model() with ModelDownloadAuthorization"
            )
        if expected_model_id is not None:
            if not expected_model_id.strip():
                raise ValueError("expected_model_id must not be empty")
            if expected_model_id != expected_model_id.strip():
                raise ValueError("expected_model_id must not contain surrounding whitespace")
        if resource_policy is not None and resource_observer is None:
            raise ValueError("resource_observer is required when resource_policy is configured")

        self._capabilities = ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )
        self._default_model = default_model
        self._app_name = app_name
        self._model_cache_dir = Path(model_cache_dir) if model_cache_dir is not None else None
        self._expected_model_id = expected_model_id
        self._resource_policy = resource_policy
        self._resource_observer = resource_observer
        self._manager_factory = manager_factory
        self._manager_instance: Any | None = None
        self._inference_lock = asyncio.Lock()
        self._model_management_lock = asyncio.Lock()
        self._owned_model_lock = Lock()
        self._owned_loaded_models: dict[str, Any] = {}

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        acquired = False
        worker: asyncio.Task[tuple[str, str, ModelUsage]] | None = None
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._inference_lock.acquire(), timeout=remaining)
            acquired = True

            self._enforce_resource_policy()

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            worker = asyncio.create_task(asyncio.to_thread(self._complete_sync, request))
            try:
                text, model_name, usage = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=remaining
                )
            except TimeoutError as exc:
                self._release_slot_when_worker_finishes(worker)
                acquired = False
                raise ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "Foundry Local inference timed out; native inference may still be finishing",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                ) from exc
            except asyncio.CancelledError:
                self._release_slot_when_worker_finishes(worker)
                acquired = False
                raise
        except TimeoutError as exc:
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "Foundry Local inference slot timed out",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except ModelGatewayError:
            raise
        except (ImportError, ModuleNotFoundError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "Foundry Local SDK is not installed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local returned an invalid response",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except Exception as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local inference failed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        finally:
            if acquired:
                self._inference_lock.release()

        return ModelResponse(
            request_id=request.request_id,
            text=text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=model_name,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def download_model(
        self,
        authorization: ModelDownloadAuthorization,
        *,
        cancel_event: Event | None = None,
        timeout_seconds: float = 1800.0,
    ) -> FoundryModelEvidence:
        """Explicitly acquire one Foundry model under a bounded authorization.

        The caller supplies product-level intent bound to provider/model/license
        evidence and may additionally pin the exact public variant ID. Download
        is never inferred from ``ModelRequest``. Foundry's documented download
        cancellation event is always used. A caller timeout signals that event
        and retains the shared provider slot until the native worker really exits.
        """
        if authorization.provider_id != self.capabilities.provider_id:
            raise ValueError(
                "download authorization provider does not match Foundry Local provider"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if (
            self._expected_model_id is not None
            and authorization.expected_model_id is not None
            and authorization.expected_model_id != self._expected_model_id
        ):
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "download authorization model identity conflicts with provider pin",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        management_acquired = False
        inference_acquired = False
        worker: asyncio.Task[None] | None = None
        effective_cancel_event = cancel_event or Event()
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._model_management_lock.acquire(), timeout=remaining)
            management_acquired = True

            model = self._get_model(authorization.model)
            expected_model_id = authorization.expected_model_id or self._expected_model_id
            self._validate_model_identity(model, expected_model_id)
            if bool(model.is_cached):
                return self._model_evidence(model)

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._inference_lock.acquire(), timeout=remaining)
            inference_acquired = True

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            worker = asyncio.create_task(
                asyncio.to_thread(model.download, cancel_event=effective_cancel_event)
            )
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
            except TimeoutError as exc:
                effective_cancel_event.set()
                self._release_slot_when_worker_finishes(worker)
                inference_acquired = False
                raise ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    (
                        f"Foundry Local model '{authorization.model}' download timed out; "
                        "native cancellation was signalled"
                    ),
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                ) from exc
            except asyncio.CancelledError:
                effective_cancel_event.set()
                self._release_slot_when_worker_finishes(worker)
                inference_acquired = False
                raise
            except Exception as exc:
                if effective_cancel_event.is_set():
                    raise ModelGatewayError(
                        ModelErrorCode.CANCELLED,
                        f"Foundry Local model '{authorization.model}' download was cancelled",
                        provider_id=self.capabilities.provider_id,
                        retryable=False,
                    ) from exc
                raise

            evidence = self._model_evidence(model)
            self._validate_model_identity(model, expected_model_id)
            if not evidence.cached:
                raise ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    f"Foundry Local model '{authorization.model}' download did not produce cache evidence",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
            return evidence
        except TimeoutError as exc:
            effective_cancel_event.set()
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                f"Foundry Local model '{authorization.model}' download slot timed out",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except asyncio.CancelledError:
            raise
        except ModelGatewayError:
            raise
        except (ImportError, ModuleNotFoundError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "Foundry Local SDK is not installed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except Exception as exc:
            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                ) from exc
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local model '{authorization.model}' download failed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        finally:
            if inference_acquired:
                self._inference_lock.release()
            if management_acquired:
                self._model_management_lock.release()

    def inspect_model(self, model_alias: str | None = None) -> FoundryModelEvidence:
        """Return read-only public-SDK metadata for release/hardware evidence."""
        alias = model_alias or self._default_model
        if not alias.strip():
            raise ValueError("model_alias must not be empty")
        if alias != alias.strip():
            raise ValueError("model_alias must not contain surrounding whitespace")
        model = self._get_model(alias)
        if self._expected_model_id is not None:
            self._validate_model_identity(model, self._expected_model_id)
        return self._model_evidence(model)

    def close(self) -> None:
        """Unload only models loaded by this provider instance.

        FoundryLocalManager is a process-wide singleton. Unloading every model in
        its catalog would allow one adapter/proof to disrupt another consumer.
        Nika therefore tracks ownership only when this instance performed the
        load. Closing while native inference/download work still owns the slot
        fails closed instead of racing an unload against active native work.
        """
        if self._inference_lock.locked():
            raise RuntimeError("cannot close Foundry Local provider while native work is active")

        with self._owned_model_lock:
            owned = tuple(self._owned_loaded_models.items())
        for model_id, model in owned:
            try:
                if bool(model.is_loaded):
                    model.unload()
            except Exception as exc:
                raise ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    f"Foundry Local model '{model_id}' could not be unloaded",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                ) from exc
            with self._owned_model_lock:
                self._owned_loaded_models.pop(model_id, None)

    def _release_slot_when_worker_finishes(self, worker: asyncio.Task[Any]) -> None:
        def release(task: asyncio.Task[Any]) -> None:
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
            if self._inference_lock.locked():
                self._inference_lock.release()

        worker.add_done_callback(release)

    def _complete_sync(self, request: ModelRequest) -> tuple[str, str, ModelUsage]:
        model_alias = request.model or self._default_model
        model = self._get_model(model_alias)
        self._validate_model_identity(model, self._expected_model_id)

        if not bool(model.is_cached):
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                (
                    f"Foundry Local model '{model_alias}' is not cached; "
                    "use the explicit model download action before inference"
                ),
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        if not bool(model.is_loaded):
            model.load()
            model_id = str(model.id)
            with self._owned_model_lock:
                self._owned_loaded_models[model_id] = model

        client = model.get_chat_client()
        if request.temperature is not None and hasattr(client, "settings"):
            client.settings.temperature = request.temperature

        response = client.complete_chat(
            [{"role": message.role, "content": message.content} for message in request.messages]
        )
        raw_text = response.choices[0].message.content
        if not isinstance(raw_text, str):
            raise TypeError("Foundry Local response content must be text")
        usage = self._usage(response)
        resolved_model = str(getattr(model, "alias", None) or model_alias)
        return raw_text, resolved_model, usage

    def _get_model(self, alias: str) -> Any:
        try:
            manager = self._manager()
            model = manager.catalog.get_model(alias)
        except (ImportError, ModuleNotFoundError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "Foundry Local SDK is not installed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        except ModelGatewayError:
            raise
        except Exception as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local catalog lookup failed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc
        if model is None:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"Foundry Local model '{alias}' is not present in the catalog",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return model

    def _model_evidence(self, model: Any) -> FoundryModelEvidence:
        model_id = str(model.id)
        cached = bool(model.is_cached)
        path: str | None = None
        if cached:
            try:
                path = str(model.get_path())
            except Exception:  # noqa: BLE001 - metadata collection must not break inference use.
                path = None
        return FoundryModelEvidence(
            model_id=model_id,
            model_version=self._version_from_model_id(model_id),
            alias=str(model.alias),
            cached=cached,
            loaded=bool(model.is_loaded),
            path=path,
            context_length=self._optional_int(getattr(model, "context_length", None)),
            input_modalities=self._optional_str(getattr(model, "input_modalities", None)),
            output_modalities=self._optional_str(getattr(model, "output_modalities", None)),
            capability_tags=self._optional_str(getattr(model, "capabilities", None)),
            supports_tool_calling=self._optional_bool(
                getattr(model, "supports_tool_calling", None)
            ),
        )

    def _validate_model_identity(self, model: Any, expected_model_id: str | None) -> None:
        if expected_model_id is None:
            return
        actual_model_id = str(model.id)
        if actual_model_id != expected_model_id:
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                (
                    "Foundry Local model identity changed: "
                    f"expected '{expected_model_id}', resolved '{actual_model_id}'"
                ),
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    def _enforce_resource_policy(self) -> None:
        policy = self._resource_policy
        if policy is None:
            return
        observer = self._resource_observer
        if observer is None:  # Constructor validation makes this defensive only.
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "model resource policy has no resource observer",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        try:
            snapshot = observer.snapshot()
        except Exception as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model resource preflight could not read the system resource snapshot",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        if policy.max_cpu_percent is not None and snapshot.cpu_percent > policy.max_cpu_percent:
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "Foundry Local inference blocked by CPU resource policy",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if (
            policy.max_memory_percent is not None
            and snapshot.memory_percent > policy.max_memory_percent
        ):
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "Foundry Local inference blocked by memory resource policy",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if (
            policy.min_available_memory_bytes is not None
            and snapshot.available_memory_bytes < policy.min_available_memory_bytes
        ):
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "Foundry Local inference blocked by available-memory resource policy",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    def _manager(self) -> Any:
        if self._manager_instance is not None:
            return self._manager_instance
        if self._manager_factory is not None:
            self._manager_instance = self._manager_factory()
            return self._manager_instance

        from foundry_local_sdk import Configuration, FoundryLocalManager

        config_kwargs: dict[str, object] = {"app_name": self._app_name}
        if self._model_cache_dir is not None:
            config_kwargs["model_cache_dir"] = str(self._model_cache_dir)
        configuration = Configuration(**config_kwargs)
        FoundryLocalManager.initialize(configuration)
        self._manager_instance = FoundryLocalManager.instance
        return self._manager_instance

    @staticmethod
    def _usage(response: Any) -> ModelUsage:
        raw = getattr(response, "usage", None)
        if raw is None:
            return ModelUsage()

        def read(*names: str) -> int | None:
            for name in names:
                value = getattr(raw, name, None)
                if value is None or isinstance(value, bool):
                    continue
                try:
                    converted = int(value)
                except (TypeError, ValueError):
                    continue
                if converted >= 0:
                    return converted
            return None

        return ModelUsage(
            input_tokens=read("prompt_tokens", "input_tokens"),
            output_tokens=read("completion_tokens", "output_tokens"),
            total_tokens=read("total_tokens"),
        )

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_bool(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        return bool(value)

    @staticmethod
    def _version_from_model_id(model_id: str) -> str | None:
        _prefix, separator, version = model_id.rpartition(":")
        if separator and version.isdigit():
            return version
        return None
