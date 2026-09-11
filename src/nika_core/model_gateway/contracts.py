from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Protocol


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


class ProviderKind(StrEnum):
    NO_LLM = "no_llm"
    LOCAL = "local"
    CLOUD = "cloud"


class ModelErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    RESOURCE_LIMIT = "resource_limit"
    PROVIDER_ERROR = "provider_error"


class ModelFailureEffect(StrEnum):
    """Effect certainty attached to a typed model-provider failure.

    NO_EFFECT is positive adapter evidence that the failed attempt did not
    start or commit a provider-side model effect. UNKNOWN remains fail-closed.
    """

    UNKNOWN = "unknown"
    NO_EFFECT = "no_effect"


_MODEL_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})


def _require_canonical_identifier(
    name: str, value: object, *, optional: bool = False
) -> str | None:
    if value is None:
        if optional:
            return None
        raise TypeError(f"{name} must be text")
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must not be empty")
    if stripped != value:
        raise ValueError(f"{name} must not contain surrounding whitespace")
    if any(not char.isprintable() for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str):
            raise TypeError("message role must be text")
        if self.role not in _MODEL_MESSAGE_ROLES:
            raise ValueError(f"unsupported message role: {self.role}")
        if not isinstance(self.content, str):
            raise TypeError("message content must be text")
        if not self.content.strip():
            raise ValueError("message content must not be empty")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    messages: tuple[ModelMessage, ...]
    model: str | None = None
    provider_id: str | None = None
    provider_kind: ProviderKind | None = None
    fallback_provider_ids: tuple[str, ...] = ()
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    timeout_seconds: float = 60.0
    temperature: float | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_canonical_identifier("request_id", self.request_id)

        if not isinstance(self.messages, (tuple, list)):
            raise TypeError("messages must be a list or tuple")
        canonical_messages = tuple(self.messages)
        if not canonical_messages:
            raise ValueError("at least one message is required")
        if any(not isinstance(message, ModelMessage) for message in canonical_messages):
            raise TypeError("messages must contain only ModelMessage values")
        object.__setattr__(self, "messages", canonical_messages)

        _require_canonical_identifier("model", self.model, optional=True)
        _require_canonical_identifier("provider_id", self.provider_id, optional=True)
        if self.provider_kind is not None and not isinstance(
            self.provider_kind, ProviderKind
        ):
            raise TypeError("provider_kind must be a ProviderKind")
        if not isinstance(self.privacy, PrivacyClass):
            raise TypeError("privacy must be a PrivacyClass")

        if not isinstance(self.fallback_provider_ids, (tuple, list)):
            raise TypeError("fallback_provider_ids must be a list or tuple")
        canonical_fallbacks = tuple(self.fallback_provider_ids)
        for provider_id in canonical_fallbacks:
            _require_canonical_identifier("fallback provider ID", provider_id)
        if len(set(canonical_fallbacks)) != len(canonical_fallbacks):
            raise ValueError("fallback provider IDs must be unique")
        if self.provider_id is not None and self.provider_id in canonical_fallbacks:
            raise ValueError("primary provider cannot also be a fallback provider")
        object.__setattr__(self, "fallback_provider_ids", canonical_fallbacks)

        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be numeric")
        try:
            finite_timeout = isfinite(float(self.timeout_seconds))
        except OverflowError:
            finite_timeout = False
        if not finite_timeout or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")

        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise TypeError("temperature must be numeric")
            try:
                finite_temperature = isfinite(float(self.temperature))
            except OverflowError:
                finite_temperature = False
            if not finite_temperature or not 0 <= self.temperature <= 2:
                raise ValueError("temperature must be finite and between 0 and 2")
            object.__setattr__(self, "temperature", float(self.temperature))

        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a string mapping")
        canonical_metadata: dict[str, str] = {}
        for key, value in self.metadata.items():
            canonical_key = _require_canonical_identifier("metadata key", key)
            assert canonical_key is not None
            if not isinstance(value, str):
                raise TypeError("metadata values must be text")
            if not value.strip():
                raise ValueError("metadata values must not be empty")
            canonical_metadata[canonical_key] = value
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(sorted(canonical_metadata.items()))),
        )


@dataclass(frozen=True, slots=True)
class ModelDownloadAuthorization:
    """Explicit product-level intent to acquire one optional model artifact.

    This object is deliberately separate from ModelRequest so ordinary inference
    can never gain model-download permission merely by selecting a model name.
    ``expected_model_id`` can pin an immutable provider artifact/variant identity
    in addition to the logical model alias. The license reference is evidence
    supplied/reviewed by the product layer; it is not inferred from the provider
    SDK license.
    """

    provider_id: str
    model: str
    license_reference: str
    expected_model_id: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if self.provider_id != self.provider_id.strip():
            raise ValueError("provider_id must not contain surrounding whitespace")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.model != self.model.strip():
            raise ValueError("model must not contain surrounding whitespace")
        if not self.license_reference.strip():
            raise ValueError("license_reference must not be empty")
        if self.license_reference != self.license_reference.strip():
            raise ValueError("license_reference must not contain surrounding whitespace")
        if self.expected_model_id is not None:
            if not self.expected_model_id.strip():
                raise ValueError("expected_model_id must not be empty")
            if self.expected_model_id != self.expected_model_id.strip():
                raise ValueError("expected_model_id must not contain surrounding whitespace")


@dataclass(frozen=True, slots=True)
class ModelResourcePolicy:
    """Fail-closed preflight budget for an in-process model operation.

    This is intentionally provider-neutral product policy. It consumes the
    existing ResourceObserverPort snapshot rather than introducing another
    system-monitor dependency or persistence schema.
    """

    max_cpu_percent: float | None = None
    max_memory_percent: float | None = None
    min_available_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_cpu_percent", self.max_cpu_percent),
            ("max_memory_percent", self.max_memory_percent),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not 0 < value <= 100:
                raise ValueError(f"{name} must be finite and in the range (0, 100]")
            try:
                finite_value = isfinite(float(value))
            except OverflowError:
                finite_value = False
            if not finite_value:
                raise ValueError(f"{name} must be finite and in the range (0, 100]")
        if self.min_available_memory_bytes is not None:
            if isinstance(self.min_available_memory_bytes, bool) or not isinstance(
                self.min_available_memory_bytes, int
            ):
                raise TypeError("min_available_memory_bytes must be an integer")
            if self.min_available_memory_bytes <= 0:
                raise ValueError("min_available_memory_bytes must be greater than zero")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    text: str
    provider_id: str
    provider_kind: ProviderKind
    model: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider_id: str
    kind: ProviderKind
    supports_private_data: bool
    supports_tools: bool = False
    supports_streaming: bool = False
    # Fail closed. A provider may opt in only after the adapter/upstream path has
    # evidence that cancelling/timing out the caller also stops the underlying
    # inference, not merely the local coroutine or HTTP socket.
    supports_hard_cancellation: bool = False


class ModelGatewayError(RuntimeError):
    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        provider_id: str | None = None,
        retryable: bool = False,
        failure_effect: ModelFailureEffect = ModelFailureEffect.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.retryable = retryable
        self.failure_effect = failure_effect


class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
