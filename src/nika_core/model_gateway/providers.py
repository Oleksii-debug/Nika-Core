from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)


def _require_provider_text(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    if not value:
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    if any(not char.isprintable() for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


class DeterministicMockProvider:
    def __init__(self, *, provider_id: str = "mock", prefix: str = "mock") -> None:
        provider_id = _require_provider_text("provider_id", provider_id)
        if type(prefix) is not str:
            raise TypeError("prefix must be exact text")
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.NO_LLM,
            supports_private_data=True,
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
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        provider_id = _require_provider_text("provider_id", provider_id)
        base_url = _require_provider_text("base_url", base_url)
        default_model = _require_provider_text("default_model", default_model)
        if type(kind) is not ProviderKind:
            raise TypeError("kind must be a ProviderKind")
        if kind is ProviderKind.NO_LLM:
            raise ValueError("HTTP provider cannot be no_llm")
        if api_key is not None and type(api_key) is not str:
            raise TypeError("api_key must be exact text when supplied")
        if type(supports_private_data) is not bool:
            raise TypeError("supports_private_data must be an exact boolean")
        if type(supports_hard_cancellation) is not bool:
            raise TypeError("supports_hard_cancellation must be an exact boolean")
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
            supports_hard_cancellation=supports_hard_cancellation,
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
            if not isinstance(body, dict) or "error" in body:
                raise ValueError("model provider did not return a successful object")
            choice = body["choices"][0]
            # This adapter returns text, not partial generations or tool calls.
            # Some compatible providers omit finish_reason; when supplied,
            # a nonterminal reason must never become a successful result.
            if "finish_reason" in choice and choice["finish_reason"] != "stop":
                raise ValueError("model response is not a completed text answer")
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
            # A legacy compatible endpoint may omit role, but an explicitly
            # non-assistant message must not be treated as the model's answer.
            if "role" in message and message["role"] != "assistant":
                raise ValueError("model response is not an assistant message")
            raw_text = message["content"]
            if not isinstance(raw_text, str):
                raise TypeError("message content must be text")
            # The provider must positively attest its model identity. Never
            # manufacture evidence from the outbound request or default.
            model = body["model"]
            if type(model) is not str or not model or model != model.strip():
                raise ValueError("provider model identity is invalid")
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
    provide that guarantee. Redirect following is explicitly disabled so the
    LOCAL privacy boundary cannot escape to another HTTP origin.
    """

    def __init__(
        self,
        *,
        default_model: str,
        base_url: str = "http://localhost:11434",
        think: bool | str = False,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        default_model = _require_provider_text("default_model", default_model)
        base_url = _require_provider_text("base_url", base_url)
        parsed = urlsplit(base_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Ollama base_url requires an HTTP(S) loopback host")
        if parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama local route must use a loopback host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Ollama base_url must not contain userinfo")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Ollama base_url must not contain path, query, or fragment")
        self._capabilities = ProviderCapabilities(
            provider_id="ollama",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
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
            async with self._client_factory(
                timeout=request.timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").partition(";")[0]
                if media_type.strip().casefold() != "application/json":
                    raise ValueError("Ollama must return a JSON response")
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
            if not isinstance(body, dict) or "error" in body:
                raise ValueError("Ollama did not return a successful object")
            if body.get("done") is not True:
                raise ValueError("Ollama returned an incomplete response")
            if "done_reason" in body and body["done_reason"] != "stop":
                raise ValueError("Ollama response was not completed normally")
            response_model = body["model"]
            if not isinstance(response_model, str) or response_model != model:
                raise ValueError("Ollama response model differs from requested model")
            raw_message = body["message"]
            if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
                raise TypeError("message must be an assistant object")
            text = raw_message["content"]
            if not isinstance(text, str):
                raise TypeError("message content must be text")
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
    if status in {401, 403}:
        return ModelErrorCode.AUTHENTICATION, False
    if status == 429:
        return ModelErrorCode.RATE_LIMITED, True
    if status >= 500:
        return ModelErrorCode.UNAVAILABLE, True
    return ModelErrorCode.PROVIDER_ERROR, False


def _normalize_ollama_think(value: bool | str) -> bool | str:
    if type(value) is bool:
        return value
    if type(value) is not str:
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
