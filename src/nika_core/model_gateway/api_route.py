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
        if type(credential_ref) is not str:
            raise TypeError("credential_ref must be text")
        if not credential_ref.startswith(self.prefix):
            raise CredentialResolutionError("credential reference scheme is unsupported")
        variable = credential_ref[len(self.prefix) :]
        if (
            not variable
            or variable != variable.strip()
            or any(not char.isprintable() for char in variable)
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
            if type(value) is not str:
                raise TypeError(f"{name} must be text")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
            if any(not char.isprintable() for char in value):
                raise ValueError(f"{name} must not contain control characters")
        if type(self.supports_private_data) is not bool:
            raise TypeError("supports_private_data must be a boolean")
        if type(self.supports_hard_cancellation) is not bool:
            raise TypeError("supports_hard_cancellation must be a boolean")

        # urlsplit silently removes CR/LF/TAB and leading C0 controls. Reject
        # unsafe route text before parsing so approved endpoint identity is stable.
        if any(not char.isprintable() or char == "\\" for char in self.base_url):
            raise ValueError("API model route base_url contains unsafe characters")
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("API model route base_url is invalid") from None
        if parsed.scheme.lower() != "https":
            raise ValueError("API model route requires HTTPS")
        if not hostname:
            raise ValueError("API model route base_url requires a host")
        if "%" in parsed.netloc or port == 0 or parsed.netloc.endswith(":"):
            raise ValueError("API model route base_url has an invalid authority")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("API model route base_url must not contain userinfo")
        if "?" in self.base_url or "#" in self.base_url:
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
        if type(config) is not ApiModelRouteConfig:
            raise TypeError("config must be an ApiModelRouteConfig")
        # ApiModelRouteConfig is caller-owned even though it is frozen: callers
        # retaining the object can still use object.__setattr__. Cross that
        # boundary once and retain only exact built-in authority/effect scalars.
        provider_id = config.provider_id
        base_url = config.base_url
        default_model = config.default_model
        credential_ref = config.credential_ref
        supports_private_data = config.supports_private_data
        supports_hard_cancellation = config.supports_hard_cancellation
        for name, value in (
            ("provider_id", provider_id),
            ("base_url", base_url),
            ("default_model", default_model),
            ("credential_ref", credential_ref),
        ):
            if type(value) is not str:
                raise TypeError(f"{name} must be exact text")
        if type(supports_private_data) is not bool:
            raise TypeError("supports_private_data must be an exact boolean")
        if type(supports_hard_cancellation) is not bool:
            raise TypeError("supports_hard_cancellation must be an exact boolean")

        # Revalidate the captured route rather than trusting a caller-owned
        # config that may already have been mutated after construction.
        snapshot = ApiModelRouteConfig(
            provider_id=provider_id,
            base_url=base_url,
            default_model=default_model,
            credential_ref=credential_ref,
            supports_private_data=supports_private_data,
            supports_hard_cancellation=supports_hard_cancellation,
        )
        parsed = urlsplit(snapshot.base_url)
        effect_network_host = parsed.hostname
        if effect_network_host is None:
            raise ValueError("API model route base_url requires a host")

        self._provider_id = snapshot.provider_id
        self._base_url = snapshot.base_url
        self._default_model = snapshot.default_model
        self._credential_ref = snapshot.credential_ref
        self._supports_private_data = snapshot.supports_private_data
        self._supports_hard_cancellation = snapshot.supports_hard_cancellation
        self._effect_network_host = effect_network_host.lower().rstrip(".")
        self._credential_resolver = credential_resolver
        self._client_factory = client_factory
        self._prototype = OpenAICompatibleProvider(
            provider_id=self._provider_id,
            base_url=self._base_url,
            kind=ProviderKind.CLOUD,
            default_model=self._default_model,
            supports_private_data=self._supports_private_data,
            supports_hard_cancellation=self._supports_hard_cancellation,
            client_factory=client_factory,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        prototype = self._prototype.capabilities
        return ProviderCapabilities(
            provider_id=prototype.provider_id,
            kind=prototype.kind,
            supports_private_data=prototype.supports_private_data,
            supports_tools=prototype.supports_tools,
            supports_streaming=prototype.supports_streaming,
            supports_hard_cancellation=prototype.supports_hard_cancellation,
            effect_network_host=self._effect_network_host,
        )

    @property
    def credential_ref(self) -> str:
        """Opaque reference for host configuration/persistence; never raw material."""

        return self._credential_ref

    async def complete(self, request: ModelRequest) -> ModelResponse:
        material = self._resolve_material()
        provider: OpenAICompatibleProvider | None = None
        safe_error: ModelGatewayError | None = None
        try:
            provider = OpenAICompatibleProvider(
                provider_id=self._provider_id,
                base_url=self._base_url,
                kind=ProviderKind.CLOUD,
                default_model=self._default_model,
                api_key=material,
                supports_private_data=self._supports_private_data,
                supports_hard_cancellation=self._supports_hard_cancellation,
                client_factory=self._client_factory,
            )
            try:
                return await provider.complete(request)
            except ModelGatewayError as error:
                safe_error = ModelGatewayError(
                    error.code,
                    str(error),
                    provider_id=error.provider_id or self._provider_id,
                    retryable=error.retryable,
                    failure_effect=error.failure_effect,
                )
            except Exception:  # noqa: BLE001 - transport/client factories are untrusted
                safe_error = ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "model provider failed",
                    provider_id=self._provider_id,
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                )
        finally:
            provider = None
            material = ""
        # Raise outside the raw provider exception handler. "from None" only
        # hides __context__ in display; it does not remove the raw exception.
        assert safe_error is not None
        raise safe_error

    def _resolve_material(self) -> str:
        material: str | None = None
        resolution_failed = False
        try:
            material = self._credential_resolver.resolve(self._credential_ref)
        except Exception:  # noqa: BLE001 - untrusted resolvers may raise arbitrary exceptions
            resolution_failed = True
        if (
            resolution_failed
            or type(material) is not str
            or not material
            or len(material) > 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in material)
        ):
            raise ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                "model credential could not be resolved",
                provider_id=self._provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        return material
