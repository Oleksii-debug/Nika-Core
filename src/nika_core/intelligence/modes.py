from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway


class IntelligenceMode(StrEnum):
    """Product-level intelligence choices with explicit trust boundaries."""

    DETERMINISTIC = "deterministic"
    EMBEDDED_LOCAL = "embedded_local"
    LOCAL_OLLAMA = "local_ollama"
    EXTERNAL_API = "external_api"


class IntelligenceModeErrorCode(StrEnum):
    MODE_DISABLED = "mode_disabled"
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
    ollama_provider_id: str = "ollama"
    external_provider_id: str | None = None
    embedded_local_enabled: bool = True
    local_ollama_enabled: bool = True
    external_api_enabled: bool = False

    def __post_init__(self) -> None:
        for name, enabled in (
            ("embedded_local_enabled", self.embedded_local_enabled),
            ("local_ollama_enabled", self.local_ollama_enabled),
            ("external_api_enabled", self.external_api_enabled),
        ):
            if type(enabled) is not bool:
                raise TypeError(f"{name} must be a boolean")

        provider_ids = {
            "embedded_provider_id": self.embedded_provider_id,
            "ollama_provider_id": self.ollama_provider_id,
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
                raise ValueError(f"{name} must be an opaque provider identity, not a URL or credential")

        if len(set(provider_ids.values())) != len(provider_ids):
            raise ValueError("intelligence mode provider IDs must be distinct")
        if self.external_api_enabled and self.external_provider_id is None:
            raise ValueError(
                "external_provider_id is required when external API mode is enabled"
            )


@dataclass(frozen=True, slots=True)
class IntelligenceModeStatus:
    """Secret-free metadata safe for diagnostics/settings presentation."""

    mode: IntelligenceMode
    enabled: bool
    provider_id: str | None
    provider_kind: ProviderKind


class DeterministicCompletionPort(Protocol):
    """Adapter boundary for model-free deterministic intelligence.

    Implementations may adapt DeterministicBrain or another deterministic
    workflow to this narrow completion shape. They do not call ModelGateway.
    """

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class IntelligenceModeRouter:
    """Route one explicit intelligence mode without cross-mode fallback."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        deterministic: DeterministicCompletionPort,
        policy: IntelligenceModePolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._deterministic = deterministic
        self._policy = policy or IntelligenceModePolicy()

    def statuses(self) -> tuple[IntelligenceModeStatus, ...]:
        """Return bounded route status with no prompt, URL or credential material."""

        return (
            IntelligenceModeStatus(
                mode=IntelligenceMode.DETERMINISTIC,
                enabled=True,
                provider_id=None,
                provider_kind=ProviderKind.NO_LLM,
            ),
            IntelligenceModeStatus(
                mode=IntelligenceMode.EMBEDDED_LOCAL,
                enabled=self._policy.embedded_local_enabled,
                provider_id=self._policy.embedded_provider_id,
                provider_kind=ProviderKind.LOCAL,
            ),
            IntelligenceModeStatus(
                mode=IntelligenceMode.LOCAL_OLLAMA,
                enabled=self._policy.local_ollama_enabled,
                provider_id=self._policy.ollama_provider_id,
                provider_kind=ProviderKind.LOCAL,
            ),
            IntelligenceModeStatus(
                mode=IntelligenceMode.EXTERNAL_API,
                enabled=self._policy.external_api_enabled,
                provider_id=self._policy.external_provider_id,
                provider_kind=ProviderKind.CLOUD,
            ),
        )

    async def complete(
        self,
        mode: IntelligenceMode,
        request: ModelRequest,
    ) -> ModelResponse:
        if not isinstance(mode, IntelligenceMode):
            raise TypeError("mode must be IntelligenceMode")
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")

        if mode is IntelligenceMode.DETERMINISTIC:
            deterministic_request = replace(
                request,
                provider_id=None,
                provider_kind=ProviderKind.NO_LLM,
                fallback_provider_ids=(),
            )
            response = await self._deterministic.complete(deterministic_request)
            self._validate_response(
                mode=mode,
                request=request,
                response=response,
                expected_kind=ProviderKind.NO_LLM,
                expected_provider_id=None,
            )
            return response

        if mode is IntelligenceMode.EMBEDDED_LOCAL:
            self._require_enabled(mode, self._policy.embedded_local_enabled)
            return await self._complete_with_gateway(
                mode=mode,
                request=request,
                provider_id=self._policy.embedded_provider_id,
                expected_kind=ProviderKind.LOCAL,
            )

        if mode is IntelligenceMode.LOCAL_OLLAMA:
            self._require_enabled(mode, self._policy.local_ollama_enabled)
            return await self._complete_with_gateway(
                mode=mode,
                request=request,
                provider_id=self._policy.ollama_provider_id,
                expected_kind=ProviderKind.LOCAL,
            )

        if mode is IntelligenceMode.EXTERNAL_API:
            self._require_enabled(mode, self._policy.external_api_enabled)
            provider_id = self._policy.external_provider_id
            if provider_id is None:
                raise IntelligenceModeError(
                    IntelligenceModeErrorCode.INVALID_CONFIGURATION,
                    "external API mode has no approved provider identity",
                    mode=mode,
                )
            return await self._complete_with_gateway(
                mode=mode,
                request=request,
                provider_id=provider_id,
                expected_kind=ProviderKind.CLOUD,
            )

        raise IntelligenceModeError(
            IntelligenceModeErrorCode.INVALID_CONFIGURATION,
            "unsupported intelligence mode",
            mode=mode,
        )

    async def _complete_with_gateway(
        self,
        *,
        mode: IntelligenceMode,
        request: ModelRequest,
        provider_id: str,
        expected_kind: ProviderKind,
    ) -> ModelResponse:
        routed_request = replace(
            request,
            provider_id=provider_id,
            provider_kind=None,
            fallback_provider_ids=(),
        )
        response = await self._gateway.complete(routed_request)
        self._validate_response(
            mode=mode,
            request=request,
            response=response,
            expected_kind=expected_kind,
            expected_provider_id=provider_id,
        )
        return response

    @staticmethod
    def _require_enabled(mode: IntelligenceMode, enabled: bool) -> None:
        if not enabled:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.MODE_DISABLED,
                f"intelligence mode is disabled: {mode.value}",
                mode=mode,
            )

    @staticmethod
    def _validate_response(
        *,
        mode: IntelligenceMode,
        request: ModelRequest,
        response: ModelResponse,
        expected_kind: ProviderKind,
        expected_provider_id: str | None,
    ) -> None:
        if not isinstance(response, ModelResponse):
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence boundary returned an invalid response",
                mode=mode,
            )
        if response.request_id != request.request_id:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response request identity does not match",
                mode=mode,
            )
        if response.provider_kind is not expected_kind:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response crossed the selected provider boundary",
                mode=mode,
            )
        if expected_provider_id is not None and response.provider_id != expected_provider_id:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response came from an unexpected provider",
                mode=mode,
            )
