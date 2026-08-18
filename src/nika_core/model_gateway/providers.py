from __future__ import annotations

import time
from collections.abc import Callable

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


class DeterministicMockProvider:
    def __init__(self, *, provider_id: str = "mock", prefix: str = "mock") -> None:
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
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if kind is ProviderKind.NO_LLM:
            raise ValueError("HTTP provider cannot be no_llm")
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=kind,
            supports_private_data=supports_private_data,
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
                {"role": message.role, "content": message.content} for message in request.messages
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
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                code = ModelErrorCode.AUTHENTICATION
                retryable = False
            elif status == 429:
                code = ModelErrorCode.RATE_LIMITED
                retryable = True
            elif status >= 500:
                code = ModelErrorCode.UNAVAILABLE
                retryable = True
            else:
                code = ModelErrorCode.PROVIDER_ERROR
                retryable = False
            raise ModelGatewayError(
                code,
                f"model provider returned HTTP {status}",
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
            text = str(body["choices"][0]["message"]["content"])
            model = str(body.get("model") or request.model or self._default_model)
            raw_usage = body.get("usage") or {}
            usage = ModelUsage(
                input_tokens=raw_usage.get("prompt_tokens"),
                output_tokens=raw_usage.get("completion_tokens"),
                total_tokens=raw_usage.get("total_tokens"),
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "model provider returned an invalid response schema",
                provider_id=self.capabilities.provider_id,
            ) from exc

        return ModelResponse(
            request_id=request.request_id,
            text=text,
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class OllamaProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        default_model: str,
        base_url: str = "http://127.0.0.1:11434/v1",
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        super().__init__(
            provider_id="ollama",
            base_url=base_url,
            kind=ProviderKind.LOCAL,
            default_model=default_model,
            supports_private_data=True,
            client_factory=client_factory,
        )
