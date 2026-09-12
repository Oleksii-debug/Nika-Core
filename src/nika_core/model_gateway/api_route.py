from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.providers import OpenAICompatibleProvider


class CredentialResolutionError(RuntimeError):
    """Safe credential-reference resolution failure without secret-bearing detail."""


class CredentialResolverPort(Protocol):
    """Host-only credential material boundary used at provider execution time."""

    def resolve(self, credential_ref: str) -> str: ...


@dataclass(frozen=True, slots=True)
class EnvironmentCredentialResolver:
    """Resolve explicit ``env:NAME`` references without persisting raw material."""

    prefix: str = "env:"

    def __post_init__(self) -> None:
        if not self.prefix:
            raise ValueError("credential reference prefix must not be empty")

    def resolve(self, credential_ref: str) -> str:
        if not isinstance(credential_ref, str):
            raise TypeError("credential_ref must be text")
        if not credential_ref.startswith(self.prefix):
            raise CredentialResolutionError("credential reference scheme is unsupported")
        variable = credential_ref[len(self.prefix) :]
        if (
            not variable
            or variable != variable.strip()
            or "\x00" in variable
            or "=" in variable
        ):
            raise CredentialResolutionError("credential reference is invalid")
        material = os.environ.get(variable)
        if not material or "\x00" in material:
            raise CredentialResolutionError("credential reference is unavailable")
        return material


@dataclass(frozen=True, slots=True)
class ApiModelRouteConfig:
    """Secret-free durable configuration for one approved OpenAI-compatible route."""

    provider_id: str
    base_url: str
    default_model: str
    credential_ref: str = field(repr=False)
    supports_private_data: bool = False
    supports_hard_cancellation: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_id", self.provider_id),
            ("base_url", self.base_url),
            ("default_model", self.default_model),
            ("credential_ref", self.credential_ref),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be text")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
            if "\x00" in value:
                raise ValueError(f"{name} must not contain NUL")
        if not isinstance(self.supports_private_data, bool):
            raise TypeError("supports_private_data must be a boolean")
        if not isinstance(self.supports_hard_cancellation, bool):
            raise TypeError("supports_hard_cancellation must be a boolean")

        parsed = urlsplit(self.base_url)
        if parsed.scheme.lower() != "https":
            raise ValueError("API model route requires HTTPS")
        if not parsed.hostname:
            raise ValueError("API model route base_url requires a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("API model route base_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("API model route base_url must not contain query or fragment")


class CredentialRefOpenAICompatibleProvider:
    """Thin credential-reference wrapper over Nika's OpenAI-compatible provider.

    The semantic ModelRequest remains provider-neutral. Only this host-side
    execution boundary resolves credential material, and the material is kept
    out of durable route configuration, requests, audit payloads, and errors.
    """

    def __init__(
        self,
        *,
        config: ApiModelRouteConfig,
        credential_resolver: CredentialResolverPort,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._credential_resolver = credential_resolver
        self._client_factory = client_factory
        self._prototype = OpenAICompatibleProvider(
            provider_id=config.provider_id,
            base_url=config.base_url,
            kind=ProviderKind.CLOUD,
            default_model=config.default_model,
            supports_private_data=config.supports_private_data,
            supports_hard_cancellation=config.supports_hard_cancellation,
            client_factory=client_factory,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._prototype.capabilities

    @property
    def credential_ref(self) -> str:
        """Opaque reference for host configuration/persistence; never raw material."""

        return self._config.credential_ref

    async def complete(self, request: ModelRequest) -> ModelResponse:
        material = self._resolve_material()
        provider: OpenAICompatibleProvider | None = None
        try:
            provider = OpenAICompatibleProvider(
                provider_id=self._config.provider_id,
                base_url=self._config.base_url,
                kind=ProviderKind.CLOUD,
                default_model=self._config.default_model,
                api_key=material,
                supports_private_data=self._config.supports_private_data,
                supports_hard_cancellation=self._config.supports_hard_cancellation,
                client_factory=self._client_factory,
            )
            try:
                return await provider.complete(request)
            except ModelGatewayError as error:
                raise ModelGatewayError(
                    error.code,
                    str(error),
                    provider_id=error.provider_id or self._config.provider_id,
                    retryable=error.retryable,
                    failure_effect=error.failure_effect,
                ) from None
        finally:
            provider = None
            material = ""

    def _resolve_material(self) -> str:
        try:
            material = self._credential_resolver.resolve(self._config.credential_ref)
        except Exception:  # noqa: BLE001 - untrusted resolvers may fail with arbitrary exception types
            raise ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                "model credential could not be resolved",
                provider_id=self._config.provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            ) from None
        if not isinstance(material, str) or not material or "\x00" in material:
            raise ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                "model credential could not be resolved",
                provider_id=self._config.provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        return material
