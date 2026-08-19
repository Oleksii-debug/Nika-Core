from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)


@dataclass(frozen=True, slots=True)
class FoundryModelEvidence:
    model_id: str
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
    completions per provider instance and applies the ModelRequest timeout to
    queueing plus inference without silently downloading a missing model.
    """

    def __init__(
        self,
        *,
        default_model: str,
        app_name: str = "NikaCore",
        model_cache_dir: str | Path | None = None,
        allow_download: bool = False,
        manager_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not default_model.strip():
            raise ValueError("default_model must not be empty")
        self._capabilities = ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )
        self._default_model = default_model
        self._app_name = app_name
        self._model_cache_dir = Path(model_cache_dir) if model_cache_dir is not None else None
        self._allow_download = allow_download
        self._manager_factory = manager_factory
        self._manager_instance: Any | None = None
        self._inference_lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with self._inference_lock:
                    text, model_name, usage = await asyncio.to_thread(
                        self._complete_sync, request
                    )
        except TimeoutError as exc:
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "Foundry Local inference timed out",
                provider_id=self.capabilities.provider_id,
                retryable=True,
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

        return ModelResponse(
            request_id=request.request_id,
            text=text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=model_name,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def inspect_model(self, model_alias: str | None = None) -> FoundryModelEvidence:
        """Return read-only SDK metadata for release/hardware evidence collection."""
        alias = model_alias or self._default_model
        if not alias.strip():
            raise ValueError("model_alias must not be empty")
        manager = self._manager()
        model = manager.catalog.get_model(alias)
        if model is None:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"Foundry Local model '{alias}' is not present in the catalog",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )
        cached = bool(model.is_cached)
        path: str | None = None
        if cached:
            try:
                path = str(model.get_path())
            except Exception:  # noqa: BLE001 - metadata collection must not break inference use.
                path = None
        return FoundryModelEvidence(
            model_id=str(model.id),
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

    def close(self) -> None:
        """Unload models loaded by the shared Foundry manager when supported."""
        manager = self._manager_instance
        if manager is None:
            return
        catalog = getattr(manager, "catalog", None)
        if catalog is None:
            return
        for model in tuple(catalog.get_loaded_models()):
            model.unload()

    def _complete_sync(self, request: ModelRequest) -> tuple[str, str, ModelUsage]:
        manager = self._manager()
        model_alias = request.model or self._default_model
        model = manager.catalog.get_model(model_alias)
        if model is None:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"Foundry Local model '{model_alias}' is not present in the catalog",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

        if not bool(model.is_cached):
            if not self._allow_download:
                raise ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    (
                        f"Foundry Local model '{model_alias}' is not cached; "
                        "explicit model download permission is required"
                    ),
                    provider_id=self.capabilities.provider_id,
                    retryable=False,
                )
            model.download()

        if not bool(model.is_loaded):
            model.load()

        client = model.get_chat_client()
        if request.temperature is not None and hasattr(client, "settings"):
            client.settings.temperature = request.temperature

        response = client.complete_chat(
            [{"role": message.role, "content": message.content} for message in request.messages]
        )
        text = str(response.choices[0].message.content)
        usage = self._usage(response)
        resolved_model = str(getattr(model, "alias", None) or model_alias)
        return text, resolved_model, usage

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
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None
            return None

        return ModelUsage(
            input_tokens=read("prompt_tokens", "input_tokens"),
            output_tokens=read("completion_tokens", "output_tokens"),
            total_tokens=read("total_tokens"),
        )

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
