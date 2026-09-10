from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class IntelligenceMode(StrEnum):
    """Product-level intelligence choices with explicit trust boundaries."""

    DETERMINISTIC = "deterministic"
    EMBEDDED_LOCAL = "embedded_local"
    EXTERNAL_LOCAL = "external_local"
    EXTERNAL_API = "external_api"


class IntelligenceModeErrorCode(StrEnum):
    MODE_DISABLED = "mode_disabled"
    DETERMINISTIC_PATH_REQUIRED = "deterministic_path_required"
    RESPONSE_MISMATCH = "response_mismatch"
    INVALID_CONFIGURATION = "invalid_configuration"


class IntelligenceModeError(RuntimeError):
    def __init__(
        self,
        code: IntelligenceModeErrorCode,
        message: str,
        *,
        mode: IntelligenceMode,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.mode = mode


@dataclass(frozen=True, slots=True)
class IntelligenceModePolicy:
    """Fail-closed policy for selecting one existing intelligence boundary.

    This object is not a settings store and does not grant credentials, model
    acquisition, tool approval or fallback authority. Local modes pin the
    canonical integrated provider IDs. External API mode is opt-in and must pin
    one exact provider identity.
    """

    embedded_provider_id: str = "foundry-local"
    external_local_provider_id: str = "ollama"
    external_provider_id: str | None = None
    embedded_local_enabled: bool = True
    external_local_enabled: bool = True
    external_api_enabled: bool = False

    def __post_init__(self) -> None:
        for name, enabled in (
            ("embedded_local_enabled", self.embedded_local_enabled),
            ("external_local_enabled", self.external_local_enabled),
            ("external_api_enabled", self.external_api_enabled),
        ):
            if type(enabled) is not bool:
                raise TypeError(f"{name} must be a boolean")

        provider_ids = {
            "embedded_provider_id": self.embedded_provider_id,
            "external_local_provider_id": self.external_local_provider_id,
        }
        if self.external_provider_id is not None:
            provider_ids["external_provider_id"] = self.external_provider_id

        for name, provider_id in provider_ids.items():
            if not isinstance(provider_id, str):
                raise TypeError(f"{name} must be text")
            if not provider_id.strip():
                raise ValueError(f"{name} must not be empty")
            if provider_id != provider_id.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
            if len(provider_id) > 128 or any(ord(char) < 32 for char in provider_id):
                raise ValueError(f"{name} is invalid")
            if "://" in provider_id or provider_id.casefold().startswith("env:"):
                raise ValueError(
                    f"{name} must be an opaque provider identity, not a URL or credential"
                )

        if len(set(provider_ids.values())) != len(provider_ids):
            raise ValueError("intelligence mode provider IDs must be distinct")
        if self.external_api_enabled and self.external_provider_id is None:
            raise ValueError(
                "external_provider_id is required when external API mode is enabled"
            )


@dataclass(frozen=True, slots=True)
class IntelligenceRoute:
    """Resolved execution boundary without credentials, prompts or provider URLs."""

    mode: IntelligenceMode
    enabled: bool
    provider_id: str | None
    provider_kind: ProviderKind
    uses_model_gateway: bool


class IntelligenceModeRouter:
    """Resolve one explicit mode and execute only model-backed routes via ModelGateway.

    Deterministic mode deliberately does not emulate a model completion. Callers
    resolve that mode and invoke the existing structured DeterministicBrain path.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        policy: IntelligenceModePolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._policy = policy or IntelligenceModePolicy()

    def statuses(self) -> tuple[IntelligenceRoute, ...]:
        """Return bounded route status with no prompt, URL or credential material."""

        return (
            IntelligenceRoute(
                mode=IntelligenceMode.DETERMINISTIC,
                enabled=True,
                provider_id=None,
                provider_kind=ProviderKind.NO_LLM,
                uses_model_gateway=False,
            ),
            IntelligenceRoute(
                mode=IntelligenceMode.EMBEDDED_LOCAL,
                enabled=self._policy.embedded_local_enabled,
                provider_id=self._policy.embedded_provider_id,
                provider_kind=ProviderKind.LOCAL,
                uses_model_gateway=True,
            ),
            IntelligenceRoute(
                mode=IntelligenceMode.EXTERNAL_LOCAL,
                enabled=self._policy.external_local_enabled,
                provider_id=self._policy.external_local_provider_id,
                provider_kind=ProviderKind.LOCAL,
                uses_model_gateway=True,
            ),
            IntelligenceRoute(
                mode=IntelligenceMode.EXTERNAL_API,
                enabled=self._policy.external_api_enabled,
                provider_id=self._policy.external_provider_id,
                provider_kind=ProviderKind.CLOUD,
                uses_model_gateway=True,
            ),
        )

    def resolve(self, mode: IntelligenceMode) -> IntelligenceRoute:
        if not isinstance(mode, IntelligenceMode):
            raise TypeError("mode must be IntelligenceMode")

        routes = {route.mode: route for route in self.statuses()}
        route = routes.get(mode)
        if route is None:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.INVALID_CONFIGURATION,
                "unsupported intelligence mode",
                mode=mode,
            )
        self._require_enabled(route)
        if route.uses_model_gateway and route.provider_id is None:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.INVALID_CONFIGURATION,
                "model-backed intelligence mode has no provider identity",
                mode=mode,
            )
        return route

    async def complete_model(
        self,
        mode: IntelligenceMode,
        request: ModelRequest,
    ) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")
        route = self.resolve(mode)
        if not route.uses_model_gateway:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.DETERMINISTIC_PATH_REQUIRED,
                "deterministic mode must execute through DeterministicBrain",
                mode=mode,
            )
        if route.provider_id is None:  # resolve() proves this for model-backed routes.
            raise AssertionError("model-backed intelligence route has no provider identity")
        self._validate_explicit_model(route=route, request=request)

        routed_request = replace(
            request,
            provider_id=route.provider_id,
            provider_kind=route.provider_kind,
            fallback_provider_ids=(),
        )
        response = await self._gateway.complete(routed_request)
        self._validate_response(
            route=route,
            request=request,
            response=response,
        )
        return response

    @staticmethod
    def _require_enabled(route: IntelligenceRoute) -> None:
        if not route.enabled:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.MODE_DISABLED,
                f"intelligence mode is disabled: {route.mode.value}",
                mode=route.mode,
            )

    @staticmethod
    def _validate_explicit_model(
        *,
        route: IntelligenceRoute,
        request: ModelRequest,
    ) -> None:
        model = request.model
        if model is None:
            return
        if (
            not isinstance(model, str)
            or not model.strip()
            or model != model.strip()
            or any(ord(char) < 32 for char in model)
        ):
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "explicit model identity is invalid",
                provider_id=route.provider_id,
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )

    @staticmethod
    def _validate_response(
        *,
        route: IntelligenceRoute,
        request: ModelRequest,
        response: ModelResponse,
    ) -> None:
        if not isinstance(response, ModelResponse):
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence boundary returned an invalid response",
                mode=route.mode,
            )
        if response.request_id != request.request_id:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response request identity does not match",
                mode=route.mode,
            )
        if response.provider_kind is not route.provider_kind:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response crossed the selected provider boundary",
                mode=route.mode,
            )
        if response.provider_id != route.provider_id:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response came from an unexpected provider",
                mode=route.mode,
            )
