from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderCostClass,
    ProviderKind,
    ProviderResourceClass,
)


class DeterministicMockProvider:
    def __init__(self, *, provider_id: str = "mock", prefix: str = "mock") -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.NO_LLM,
            supports_private_data=True,
            cost_class=ProviderCostClass.NONE,
            resource_class=ProviderResourceClass.NONE,
        )
        self._prefix = prefix

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text = f"{self._prefix}: {request.messages[-1].content}"
        return ModelResponse(
            request_id=request.request_id,
            text=text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "deterministic",
        )


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        kind: ProviderKind,
        default_model: str,
        api_key: str | None = None,
        supports_private_data: bool = False,
        supports_hard_cancellation: bool = False,
        cost_class: ProviderCostClass | None = None,
        resource_class: ProviderResourceClass | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if kind is ProviderKind.NO_LLM:
            raise ValueError("HTTP provider cannot be no_llm")
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not default_model.strip():
            raise ValueError("default_model must not be empty")
        default_cost = (
            ProviderCostClass.LOCAL_RESOURCE
            if kind is ProviderKind.LOCAL
            else ProviderCostClass.METERED
        )
        default_resource = (
            ProviderResourceClass.LOCAL_SERVICE
            if kind is ProviderKind.LOCAL
            else ProviderResourceClass.REMOTE_SERVICE
        )
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
            supports_hard_cancellation=supports_hard_cancellation,
            cost_class=cost_class or default_cost,
            resource_class=resource_class or default_resource,
        )
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._api_key = api_key
        self._client_factory = client_factory

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, object] = {
            "model": request.model or self._default_model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        started = time.perf_counter()
        try:
            async with self._client_factory(timeout=request.timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "model provider timed out",
                provider_id=self.capabilities.provider_id,
                retryable=self.capabilities.supports_hard_cancellation,
            ) from exc
        except httpx.HTTPStatusError as exc:
            code, retryable = _classify_http_status(exc.response.status_code)
            if code is ModelErrorCode.TIMEOUT:
                retryable = self.capabilities.supports_hard_cancellation
            raise ModelGatewayError(
                code,
                f"model provider returned HTTP {exc.response.status_code}",
                provider_id=self.capabilities.provider_id,
                retryable=retryable,
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider response could not be processed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        try:
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            raw_choices = body["choices"]
            if not isinstance(raw_choices, list) or not raw_choices:
                raise TypeError("choices must be a non-empty list")
            raw_choice = raw_choices[0]
            if not isinstance(raw_choice, dict):
                raise TypeError("choice must be an object")
            raw_message = raw_choice["message"]
            if not isinstance(raw_message, dict):
                raise TypeError("message must be an object")
            raw_text = raw_message["content"]
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise TypeError("message content must be non-empty text")
            raw_model = body.get("model")
            if raw_model is not None and not isinstance(raw_model, str):
                raise TypeError("model must be text")
            model = raw_model or request.model or self._default_model
            if not model.strip():
                raise ValueError("model must not be empty")
            raw_usage = body.get("usage")
            if raw_usage is None:
                raw_usage = {}
            elif not isinstance(raw_usage, dict):
                raise TypeError("usage must be an object")
            usage = ModelUsage(
                input_tokens=_optional_int(raw_usage.get("prompt_tokens")),
                output_tokens=_optional_int(raw_usage.get("completion_tokens")),
                total_tokens=_optional_int(raw_usage.get("total_tokens")),
            )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid response schema",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        return ModelResponse(
            request_id=request.request_id,
            text=raw_text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class OllamaProvider:
    """Native Ollama `/api/chat` adapter behind Nika's stable provider contract.

    Ordinary Nika requests intentionally disable Ollama streaming. Thinking is
    disabled by default for models that support a boolean switch; callers may
    explicitly select a documented Ollama thinking level for models such as
    GPT-OSS. The reasoning trace is still not copied into Nika's shared
    response contract. Client cancellation is not represented as hard
    server-side inference cancellation because the native Ollama API does not
    provide that guarantee.
    """

    def __init__(
        self,
        *,
        default_model: str,
        base_url: str = "http://localhost:11434",
        think: bool | str = False,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if not default_model.strip():
            raise ValueError("default_model must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        self._capabilities = ProviderCapabilities(
            provider_id="ollama",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
            cost_class=ProviderCostClass.LOCAL_RESOURCE,
            resource_class=ProviderResourceClass.LOCAL_SERVICE,
        )
        self._default_model = default_model
        self._base_url = base_url.rstrip("/")
        self._think = _normalize_ollama_think(think)
        self._client_factory = client_factory

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self._default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "stream": False,
            "think": self._think,
        }
        if request.temperature is not None:
            payload["options"] = {"temperature": request.temperature}

        started = time.perf_counter()
        try:
            async with self._client_factory(timeout=request.timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "Ollama timed out",
                provider_id=self.capabilities.provider_id,
                retryable=self.capabilities.supports_hard_cancellation,
            ) from exc
        except httpx.HTTPStatusError as exc:
            code, retryable = _classify_http_status(exc.response.status_code)
            if code is ModelErrorCode.TIMEOUT:
                retryable = self.capabilities.supports_hard_cancellation
            raise ModelGatewayError(
                code,
                f"Ollama returned HTTP {exc.response.status_code}",
                provider_id=self.capabilities.provider_id,
                retryable=retryable,
            ) from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Ollama response could not be processed",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        try:
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            raw_message = body["message"]
            if not isinstance(raw_message, dict):
                raise TypeError("message must be an object")
            text = raw_message["content"]
            if not isinstance(text, str) or not text.strip():
                raise TypeError("message content must be non-empty text")
            raw_model = body.get("model")
            if raw_model is not None and not isinstance(raw_model, str):
                raise TypeError("model must be text")
            response_model = raw_model or model
            if not response_model.strip():
                raise ValueError("model must not be empty")
            prompt_tokens = _optional_int(body.get("prompt_eval_count"))
            output_tokens = _optional_int(body.get("eval_count"))
            usage = ModelUsage(
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=_sum_optional(prompt_tokens, output_tokens),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Ollama returned an invalid response schema",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            ) from exc

        return ModelResponse(
            request_id=request.request_id,
            text=text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=response_model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def _classify_http_status(status: int) -> tuple[ModelErrorCode, bool]:
    if status == 401:
        return ModelErrorCode.AUTHENTICATION, False
    if status in {402, 403}:
        return ModelErrorCode.POLICY_DENIED, False
    if status in {400, 404, 422}:
        return ModelErrorCode.INVALID_REQUEST, False
    if status in {413, 507}:
        return ModelErrorCode.RESOURCE_LIMIT, False
    if status == 408:
        return ModelErrorCode.TIMEOUT, False
    if status == 429:
        return ModelErrorCode.RATE_LIMITED, True
    if status >= 500:
        return ModelErrorCode.UNAVAILABLE, True
    return ModelErrorCode.PROVIDER_ERROR, False


def _normalize_ollama_think(value: bool | str) -> bool | str:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise TypeError("think must be a boolean or an Ollama thinking level")
    level = value.strip().lower()
    if level not in {"low", "medium", "high"}:
        raise ValueError("think level must be one of: low, medium, high")
    return level


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("token count must be an integer")
    if value < 0:
        raise ValueError("token count must not be negative")
    return value


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right
