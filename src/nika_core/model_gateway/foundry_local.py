from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType
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
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


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
        if type(default_model) is not str:
            raise TypeError("default_model must be text")
        if not default_model.strip():
            raise ValueError("default_model must not be empty")
        if default_model != default_model.strip():
            raise ValueError("default_model must not contain surrounding whitespace")
        if any(not char.isprintable() for char in default_model):
            raise ValueError("default_model must not contain control characters")
        if type(app_name) is not str:
            raise TypeError("app_name must be text")
        if not app_name.strip():
            raise ValueError("app_name must not be empty")
        if app_name != app_name.strip():
            raise ValueError("app_name must not contain surrounding whitespace")
        if any(not char.isprintable() for char in app_name):
            raise ValueError("app_name must not contain control characters")
        if type(allow_download) is not bool:
            raise TypeError("allow_download must be boolean")
        if allow_download:
            raise ValueError(
                "allow_download on FoundryLocalProvider is no longer supported; "
                "use download_model() with ModelDownloadAuthorization"
            )
        if expected_model_id is not None:
            if type(expected_model_id) is not str:
                raise TypeError("expected_model_id must be text")
            if not expected_model_id.strip():
                raise ValueError("expected_model_id must not be empty")
            if expected_model_id != expected_model_id.strip():
                raise ValueError("expected_model_id must not contain surrounding whitespace")
            if any(not char.isprintable() for char in expected_model_id):
                raise ValueError("expected_model_id must not contain control characters")
        if resource_policy is not None:
            resource_policy = self._snapshot_resource_policy(resource_policy)
            if resource_observer is None:
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
        self._manager_lock = Lock()
        self._inference_lock = asyncio.Lock()
        self._model_management_lock = asyncio.Lock()
        self._owned_model_lock = Lock()
        self._owned_loaded_models: dict[int, tuple[str, Any]] = {}
        self._tainted_loaded_models: dict[int, Any] = {}

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request = self._snapshot_request(request)
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        acquired = False
        worker: asyncio.Task[tuple[str, str, ModelUsage]] | None = None
        abandon_event = Event()
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
            worker = asyncio.create_task(
                asyncio.to_thread(self._complete_sync, request, abandon_event)
            )
            try:
                text, model_name, usage = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=remaining
                )
            except TimeoutError as exc:
                abandon_event.set()
                self._release_slot_when_worker_finishes(worker)
                acquired = False
                raise ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "Foundry Local inference timed out; native inference may still be finishing",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                ) from exc
            except asyncio.CancelledError:
                abandon_event.set()
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
        and retains the shared provider/model-management slots until the native
        worker really exits.
        """
        authorization = self._snapshot_download_authorization(authorization)
        if authorization.provider_id != self.capabilities.provider_id:
            raise ValueError(
                "download authorization provider does not match Foundry Local provider"
            )
        if type(timeout_seconds) not in (int, float):
            raise TypeError("timeout_seconds must be numeric")
        try:
            bounded_timeout = float(timeout_seconds)
        except OverflowError:
            bounded_timeout = float("inf")
        if not isfinite(bounded_timeout) or bounded_timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if cancel_event is not None and type(cancel_event) is not Event:
            raise TypeError("cancel_event must be exact threading.Event")
        effective_cancel_event = cancel_event if cancel_event is not None else Event()
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
        if effective_cancel_event.is_set():
            raise ModelGatewayError(
                ModelErrorCode.CANCELLED,
                f"Foundry Local model '{authorization.model}' download was cancelled",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + bounded_timeout
        management_acquired = False
        inference_acquired = False
        worker: asyncio.Task[None] | None = None
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._model_management_lock.acquire(), timeout=remaining)
            management_acquired = True

            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )

            model = self._get_model(authorization.model)
            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
            # Synchronous SDK catalog/manager work consumes the same deadline.
            if deadline - loop.time() <= 0:
                raise TimeoutError
            # Capture the first resolved artifact when no explicit release pin exists.
            expected_model_id = (
                authorization.expected_model_id
                or self._expected_model_id
                or self._sdk_text(model, "id")
            )
            self._validate_model_identity(model, expected_model_id)
            if self._sdk_bool(model, "is_cached"):
                evidence = self._model_evidence(
                    model,
                    expected_alias=authorization.model,
                    expected_model_id=expected_model_id,
                )
                if effective_cancel_event.is_set():
                    raise ModelGatewayError(
                        ModelErrorCode.CANCELLED,
                        f"Foundry Local model '{authorization.model}' download was cancelled",
                        provider_id=self.capabilities.provider_id,
                        retryable=False,
                    )
                # Cached-model SDK evidence getters may also exceed the deadline.
                if deadline - loop.time() <= 0:
                    raise TimeoutError
                return evidence

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._inference_lock.acquire(), timeout=remaining)
            inference_acquired = True

            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            # Metadata getters may block, exhaust the budget or observe cancellation.
            # Fence the native download effect only after those getters return.
            self._validate_model_alias(model, authorization.model)
            self._validate_model_identity(model, expected_model_id)
            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
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
                self._release_slot_when_worker_finishes(
                    worker, release_model_management=True
                )
                inference_acquired = False
                management_acquired = False
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
                self._release_slot_when_worker_finishes(
                    worker, release_model_management=True
                )
                inference_acquired = False
                management_acquired = False
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

            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
            self._validate_model_alias(model, authorization.model)
            evidence = self._model_evidence(
                model,
                expected_alias=authorization.model,
                expected_model_id=expected_model_id,
            )
            self._validate_model_identity(model, expected_model_id)
            if effective_cancel_event.is_set():
                raise ModelGatewayError(
                    ModelErrorCode.CANCELLED,
                    f"Foundry Local model '{authorization.model}' download was cancelled",
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
            # A download succeeds only if evidence was checked in the original budget.
            if deadline - loop.time() <= 0:
                raise TimeoutError
            if not evidence.cached:
                if effective_cancel_event.is_set():
                    raise ModelGatewayError(
                        ModelErrorCode.CANCELLED,
                        f"Foundry Local model '{authorization.model}' download was cancelled",
                        provider_id=self.capabilities.provider_id,
                        retryable=False,
                    )
                raise ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    (
                        f"Foundry Local model '{authorization.model}' download did not "
                        "produce cache evidence"
                    ),
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

    @staticmethod
    def _snapshot_resource_policy(raw: object) -> ModelResourcePolicy:
        """Detach resource limits from caller-owned mutable policy state."""

        if type(raw) is not ModelResourcePolicy:
            raise TypeError("resource_policy must be exact ModelResourcePolicy")
        return ModelResourcePolicy(
            max_cpu_percent=raw.max_cpu_percent,
            max_memory_percent=raw.max_memory_percent,
            min_available_memory_bytes=raw.min_available_memory_bytes,
        )

    @staticmethod
    def _snapshot_request(raw: object) -> ModelRequest:
        """Detach inference authority from caller-owned mutable request state."""

        if type(raw) is not ModelRequest:
            raise TypeError("request must be exact ModelRequest")
        if type(raw.messages) is not tuple:
            raise TypeError("request messages must be a canonical tuple")
        if type(raw.fallback_provider_ids) is not tuple:
            raise TypeError("fallback provider IDs must be a canonical tuple")
        if type(raw.metadata) is not MappingProxyType:
            raise TypeError("request metadata must be a canonical mapping")

        return ModelRequest(
            request_id=raw.request_id,
            messages=raw.messages,
            model=raw.model,
            provider_id=raw.provider_id,
            provider_kind=raw.provider_kind,
            fallback_provider_ids=raw.fallback_provider_ids,
            privacy=raw.privacy,
            timeout_seconds=raw.timeout_seconds,
            temperature=raw.temperature,
            metadata=dict(raw.metadata),
        )

    @staticmethod
    def _snapshot_download_authorization(
        raw: object,
    ) -> ModelDownloadAuthorization:
        """Detach explicit acquisition authority from caller-owned mutable state."""

        if type(raw) is not ModelDownloadAuthorization:
            raise TypeError("authorization must be exact ModelDownloadAuthorization")
        return ModelDownloadAuthorization(
            provider_id=raw.provider_id,
            model=raw.model,
            license_reference=raw.license_reference,
            expected_model_id=raw.expected_model_id,
        )

    def inspect_model(self, model_alias: str | None = None) -> FoundryModelEvidence:
        """Return read-only public-SDK metadata for release/hardware evidence."""
        if model_alias is None:
            alias = self._default_model
        else:
            if type(model_alias) is not str:
                raise TypeError("model_alias must be text")
            if not model_alias.strip():
                raise ValueError("model_alias must not be empty")
            if model_alias != model_alias.strip():
                raise ValueError("model_alias must not contain surrounding whitespace")
            if any(not char.isprintable() for char in model_alias):
                raise ValueError("model_alias must not contain control characters")
            alias = model_alias
        model = self._get_model(alias)
        if self._expected_model_id is not None:
            self._validate_model_identity(model, self._expected_model_id)
        return self._model_evidence(
            model,
            expected_alias=alias,
            expected_model_id=self._expected_model_id,
        )

    def close(self) -> None:
        """Unload only models loaded by this provider instance.

        FoundryLocalManager is a process-wide singleton. Unloading every model in
        its catalog would allow one adapter/proof to disrupt another consumer.
        Nika therefore tracks ownership only when this instance performed the
        load. Closing while native inference/download or model-management work
        still owns a slot fails closed instead of racing an unload.
        """
        if self._inference_lock.locked() or self._model_management_lock.locked():
            raise RuntimeError("cannot close Foundry Local provider while native work is active")

        first_failure: Exception | None = None

        with self._owned_model_lock:
            owned = tuple(self._owned_loaded_models.items())
        for marker, (model_id, model) in owned:
            try:
                if self._sdk_bool(model, "is_loaded"):
                    model.unload()
                    if self._sdk_bool(model, "is_loaded"):
                        raise RuntimeError(
                            f"Foundry Local model '{model_id}' remained loaded after unload"
                        )
            except Exception as exc:  # noqa: BLE001 - continue provider cleanup
                if first_failure is None:
                    first_failure = exc
                continue
            with self._owned_model_lock:
                self._owned_loaded_models.pop(marker, None)

        for marker, model in tuple(self._tainted_loaded_models.items()):
            try:
                if self._sdk_bool(model, "is_loaded"):
                    model.unload()
                    if self._sdk_bool(model, "is_loaded"):
                        raise RuntimeError(
                            "Foundry Local tainted model remained loaded after unload"
                        )
            except Exception as exc:  # noqa: BLE001 - continue provider cleanup
                if first_failure is None:
                    first_failure = exc
                continue
            self._tainted_loaded_models.pop(marker, None)

        if first_failure is not None:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local provider could not unload every owned or tainted model",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from first_failure

    def _release_slot_when_worker_finishes(
        self,
        worker: asyncio.Task[Any],
        *,
        release_model_management: bool = False,
    ) -> None:
        def release(task: asyncio.Task[Any]) -> None:
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
            if self._inference_lock.locked():
                self._inference_lock.release()
            if release_model_management and self._model_management_lock.locked():
                self._model_management_lock.release()

        worker.add_done_callback(release)

    def _complete_sync(
        self,
        request: ModelRequest,
        abandon_event: Event,
    ) -> tuple[str, str, ModelUsage]:
        model_alias = request.model or self._default_model
        model = self._get_model(model_alias)
        if id(model) in self._tainted_loaded_models:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local model has unresolved failed-load state",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        # A logical alias must not silently change its actual model mid-attempt.
        operation_model_id = self._expected_model_id or self._sdk_text(model, "id")
        self._validate_model_identity(model, operation_model_id)

        if not self._sdk_bool(model, "is_cached"):
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                (
                    f"Foundry Local model '{model_alias}' is not cached; "
                    "use the explicit model download action before inference"
                ),
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        already_loaded = self._sdk_bool(model, "is_loaded")
        self._validate_model_alias(model, model_alias)
        self._validate_model_identity(model, operation_model_id)
        if abandon_event.is_set():
            raise ModelGatewayError(
                ModelErrorCode.CANCELLED,
                "Foundry Local inference was abandoned before model load",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if not already_loaded:
            try:
                model.load()
                if not self._sdk_bool(model, "is_loaded"):
                    raise ModelGatewayError(
                        ModelErrorCode.PROVIDER_ERROR,
                        "Foundry Local model load completed without READY evidence",
                        provider_id=self.capabilities.provider_id,
                        retryable=False,
                    )
                self._validate_model_alias(model, model_alias)
                self._validate_model_identity(model, operation_model_id)
                model_id = self._sdk_text(model, "id")
            except Exception:
                self._cleanup_failed_load(model)
                raise
            self._tainted_loaded_models.pop(id(model), None)
            with self._owned_model_lock:
                self._owned_loaded_models[id(model)] = (model_id, model)

        if abandon_event.is_set():
            raise ModelGatewayError(
                ModelErrorCode.CANCELLED,
                "Foundry Local inference was abandoned before chat execution",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        client = model.get_chat_client()
        self._validate_model_alias(model, model_alias)
        self._validate_model_identity(model, operation_model_id)
        if request.temperature is not None and hasattr(client, "settings"):
            client.settings.temperature = request.temperature

        # SDK client setup and setters may block, retarget the artifact or
        # outlive the caller's deadline. Never start chat after abandonment.
        self._validate_model_alias(model, model_alias)
        self._validate_model_identity(model, operation_model_id)
        if abandon_event.is_set():
            raise ModelGatewayError(
                ModelErrorCode.CANCELLED,
                "Foundry Local inference was abandoned before chat execution",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        response = client.complete_chat(
            [{"role": message.role, "content": message.content} for message in request.messages]
        )
        self._validate_model_alias(model, model_alias)
        self._validate_model_identity(model, operation_model_id)
        raw_text = response.choices[0].message.content
        if type(raw_text) is not str:
            raise TypeError("Foundry Local response content must be text")
        usage = self._usage(response)
        # Usage and final identity getters can mutate the SDK's model alias.
        # Bind the result to the selected alias, not late mutable metadata.
        self._validate_model_alias(model, model_alias)
        self._validate_model_identity(model, operation_model_id)
        self._validate_model_alias(model, model_alias)
        return raw_text, model_alias, usage

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
        self._validate_model_alias(model, alias)
        return model

    def _model_evidence(
        self,
        model: Any,
        *,
        expected_alias: str,
        expected_model_id: str | None,
    ) -> FoundryModelEvidence:
        model_id = self._sdk_text(model, "id")
        cached = self._sdk_bool(model, "is_cached")
        path: str | None = None
        if cached:
            try:
                raw_path = model.get_path()
            except Exception:  # noqa: BLE001 - path evidence is optional metadata.
                path = None
            else:
                if type(raw_path) is not str:
                    raise ModelGatewayError(
                        ModelErrorCode.PROVIDER_ERROR,
                        "Foundry Local SDK returned invalid path metadata",
                        provider_id=self.capabilities.provider_id,
                        retryable=False,
                    )
                path = raw_path
        evidence = FoundryModelEvidence(
            model_id=model_id,
            model_version=self._version_from_model_id(model_id),
            alias=self._sdk_text(model, "alias"),
            cached=cached,
            loaded=self._sdk_bool(model, "is_loaded"),
            path=path,
            context_length=self._sdk_optional_int(model, "context_length"),
            input_modalities=self._sdk_optional_text(model, "input_modalities"),
            output_modalities=self._sdk_optional_text(model, "output_modalities"),
            capability_tags=self._sdk_optional_text(model, "capabilities"),
            supports_tool_calling=self._sdk_optional_bool(model, "supports_tool_calling"),
        )
        if evidence.alias != expected_alias:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local model alias changed while collecting evidence",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if expected_model_id is not None and evidence.model_id != expected_model_id:
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "Foundry Local model identity changed while collecting evidence",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        self._validate_model_alias(model, expected_alias)
        # Recheck the initially observed ID even when the caller omitted a pin.
        self._validate_model_identity(model, expected_model_id or model_id)
        return evidence

    def _validate_model_alias(self, model: Any, expected_alias: str) -> None:
        actual_alias = self._sdk_text(model, "alias")
        if actual_alias != expected_alias:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Foundry Local model alias changed across the native effect boundary",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    def _validate_model_identity(self, model: Any, expected_model_id: str | None) -> None:
        if expected_model_id is None:
            return
        actual_model_id = self._sdk_text(model, "id")
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

    def _cleanup_failed_load(self, model: Any) -> None:
        marker = id(model)
        self._tainted_loaded_models[marker] = model
        try:
            if self._sdk_bool(model, "is_loaded"):
                model.unload()
                if self._sdk_bool(model, "is_loaded"):
                    return
            self._tainted_loaded_models.pop(marker, None)
        except Exception:  # noqa: BLE001 - original load failure remains authoritative.
            return

    def _sdk_text(self, model: Any, field: str) -> str:
        value = getattr(model, field, None)
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or any(not char.isprintable() for char in value)
        ):
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local SDK returned invalid {field} metadata",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return value

    def _sdk_bool(self, model: Any, field: str) -> bool:
        value = getattr(model, field, None)
        if type(value) is not bool:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local SDK returned invalid {field} metadata",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return value

    def _sdk_optional_int(self, model: Any, field: str) -> int | None:
        value = getattr(model, field, None)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local SDK returned invalid {field} metadata",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return value

    def _sdk_optional_text(self, model: Any, field: str) -> str | None:
        value = getattr(model, field, None)
        if value is None:
            return None
        if type(value) is not str:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local SDK returned invalid {field} metadata",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return value

    def _sdk_optional_bool(self, model: Any, field: str) -> bool | None:
        value = getattr(model, field, None)
        if value is None:
            return None
        if type(value) is not bool:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                f"Foundry Local SDK returned invalid {field} metadata",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        return value

    @staticmethod
    def _resource_snapshot_values(raw: object) -> tuple[float, float, int]:
        if type(raw) is not ResourceSnapshot:
            raise TypeError("resource observer must return exact ResourceSnapshot")

        def percent(name: str, value: object) -> float:
            if type(value) not in (int, float):
                raise TypeError(f"{name} must be numeric")
            try:
                normalized = float(value)
            except OverflowError as exc:
                raise ValueError(f"{name} must be finite") from exc
            if not isfinite(normalized) or not 0 <= normalized <= 100:
                raise ValueError(f"{name} must be finite and in the range [0, 100]")
            return normalized

        cpu_percent = percent("cpu_percent", raw.cpu_percent)
        memory_percent = percent("memory_percent", raw.memory_percent)
        available_memory_bytes = raw.available_memory_bytes
        if type(available_memory_bytes) is not int or available_memory_bytes < 0:
            raise ValueError("available_memory_bytes must be a nonnegative integer")
        return cpu_percent, memory_percent, available_memory_bytes

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
            cpu_percent, memory_percent, available_memory_bytes = (
                self._resource_snapshot_values(snapshot)
            )
        except Exception as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model resource preflight returned an invalid system resource snapshot",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        if policy.max_cpu_percent is not None and cpu_percent > policy.max_cpu_percent:
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "Foundry Local inference blocked by CPU resource policy",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if (
            policy.max_memory_percent is not None
            and memory_percent > policy.max_memory_percent
        ):
            raise ModelGatewayError(
                ModelErrorCode.RESOURCE_LIMIT,
                "Foundry Local inference blocked by memory resource policy",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        if (
            policy.min_available_memory_bytes is not None
            and available_memory_bytes < policy.min_available_memory_bytes
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

        with self._manager_lock:
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
                if type(value) is int and value >= 0:
                    return value
            return None

        return ModelUsage(
            input_tokens=read("prompt_tokens", "input_tokens"),
            output_tokens=read("completion_tokens", "output_tokens"),
            total_tokens=read("total_tokens"),
        )

    @staticmethod
    def _version_from_model_id(model_id: str) -> str | None:
        _prefix, separator, version = model_id.rpartition(":")
        if separator and version.isdigit():
            return version
        return None
