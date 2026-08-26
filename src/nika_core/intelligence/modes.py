from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from nika_core.model_gateway.contracts import ModelRequest, ModelResponse, ProviderKind
from nika_core.model_gateway.gateway import ModelGateway


class IntelligenceMode(StrEnum):
    """Product-visible intelligence modes with explicit trust boundaries."""

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
    """Fail-closed product policy for selecting one intelligence boundary.

    Local provider identifiers match the existing Nika model adapters. External
    APIs remain disabled until the product layer explicitly enables that mode.
    This policy never grants a model download, credential, or high-impact tool
    approval; those remain governed by their existing dedicated boundaries.
    """

    embedded_provider_id: str = "foundry-local"
    ollama_provider_id: str = "ollama"
    embedded_local_enabled: bool = True
    local_ollama_enabled: bool = True
    external_api_enabled: bool = False

    def __post_init__(self) -> None:
        for name, provider_id in (
            ("embedded_provider_id", self.embedded_provider_id),
            ("ollama_provider_id", self.ollama_provider_id),
        ):
            if not provider_id.strip():
                raise ValueError(f"{name} must not be empty")
            if provider_id != provider_id.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        if self.embedded_provider_id == self.ollama_provider_id:
            raise ValueError("embedded and Ollama provider IDs must be distinct")


@dataclass(frozen=True, slots=True)
class IntelligenceModeStatus:
    """Secret-free mode metadata suitable for settings/accessibility surfaces."""

    mode: IntelligenceMode
    enabled: bool
    provider_id: str | None
    provider_kind: ProviderKind


class DeterministicCompletionPort(Protocol):
    """Non-model completion boundary used by deterministic intelligence mode."""

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class IntelligenceModeRouter:
    """Route exactly one requested intelligence mode without cross-mode fallback."""

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
        """Return configuration state without prompts, credentials, URLs, or tokens."""

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
                provider_id=None,
                provider_kind=ProviderKind.CLOUD,
            ),
        )

    async def complete(self, mode: IntelligenceMode, request: ModelRequest) -> ModelResponse:
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
                provider_kind=None,
                expected_kind=ProviderKind.LOCAL,
            )

        if mode is IntelligenceMode.LOCAL_OLLAMA:
            self._require_enabled(mode, self._policy.local_ollama_enabled)
            return await self._complete_with_gateway(
                mode=mode,
                request=request,
                provider_id=self._policy.ollama_provider_id,
                provider_kind=None,
                expected_kind=ProviderKind.LOCAL,
            )

        if mode is IntelligenceMode.EXTERNAL_API:
            self._require_enabled(mode, self._policy.external_api_enabled)
            return await self._complete_with_gateway(
                mode=mode,
                request=request,
                provider_id=None,
                provider_kind=ProviderKind.CLOUD,
                expected_kind=ProviderKind.CLOUD,
            )

        raise IntelligenceModeError(
            IntelligenceModeErrorCode.INVALID_CONFIGURATION,
            f"unsupported intelligence mode: {mode!r}",
            mode=mode,
        )

    async def _complete_with_gateway(
        self,
        *,
        mode: IntelligenceMode,
        request: ModelRequest,
        provider_id: str | None,
        provider_kind: ProviderKind | None,
        expected_kind: ProviderKind,
    ) -> ModelResponse:
        routed_request = replace(
            request,
            provider_id=provider_id,
            provider_kind=provider_kind,
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
        if response.request_id != request.request_id:
            raise IntelligenceModeError(
                IntelligenceModeErrorCode.RESPONSE_MISMATCH,
                "intelligence response request_id does not match the request",
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
