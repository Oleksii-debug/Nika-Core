from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


class ProviderKind(StrEnum):
    NO_LLM = "no_llm"
    LOCAL = "local"
    CLOUD = "cloud"


class ProviderCostClass(StrEnum):
    NONE = "none"
    LOCAL_RESOURCE = "local_resource"
    METERED = "metered"
    UNKNOWN = "unknown"


class ProviderResourceClass(StrEnum):
    NONE = "none"
    LOCAL_PROCESS = "local_process"
    LOCAL_SERVICE = "local_service"
    REMOTE_SERVICE = "remote_service"
    UNKNOWN = "unknown"


class ModelErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    AUTHENTICATION = "authentication"
    POLICY_DENIED = "policy_denied"
    RATE_LIMITED = "rate_limited"
    RESOURCE_LIMIT = "resource_limit"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {self.role}")
        if not self.content:
            raise ValueError("message content must not be empty")


@dataclass(frozen=True, slots=True)
class ModelRoutePolicy:
    """Provider-neutral routing constraints evaluated before any provider sees payload data."""

    local_only: bool = False
    allow_metered: bool = True
    allowed_resource_classes: frozenset[ProviderResourceClass] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        if not isinstance(self.local_only, bool):
            raise TypeError("local_only must be a boolean")
        if not isinstance(self.allow_metered, bool):
            raise TypeError("allow_metered must be a boolean")
        if not isinstance(self.allowed_resource_classes, frozenset):
            raise TypeError("allowed_resource_classes must be a frozenset")
        if any(
            not isinstance(resource_class, ProviderResourceClass)
            for resource_class in self.allowed_resource_classes
        ):
            raise TypeError(
                "allowed_resource_classes must contain ProviderResourceClass values"
            )


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
    metadata: dict[str, str] = field(default_factory=dict)
    route_policy: ModelRoutePolicy = field(default_factory=ModelRoutePolicy)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.messages:
            raise ValueError("at least one message is required")
        if any(not provider_id.strip() for provider_id in self.fallback_provider_ids):
            raise ValueError("fallback provider IDs must not be empty")
        if len(set(self.fallback_provider_ids)) != len(self.fallback_provider_ids):
            raise ValueError("fallback provider IDs must be unique")
        if self.provider_id is not None and self.provider_id in self.fallback_provider_ids:
            raise ValueError("primary provider cannot also be a fallback provider")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be numeric")
        if not math.isfinite(float(self.timeout_seconds)):
            raise ValueError("timeout_seconds must be finite")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise TypeError("temperature must be numeric")
            if not math.isfinite(float(self.temperature)):
                raise ValueError("temperature must be finite")
            if not 0 <= self.temperature <= 2:
                raise ValueError("temperature must be between 0 and 2")


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
            if value is not None and not 0 < value <= 100:
                raise ValueError(f"{name} must be in the range (0, 100]")
        if self.min_available_memory_bytes is not None and self.min_available_memory_bytes <= 0:
            raise ValueError("min_available_memory_bytes must be greater than zero")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.total_tokens is not None:
            known_total = sum(
                value
                for value in (self.input_tokens, self.output_tokens)
                if value is not None
            )
            if known_total and self.total_tokens < known_total:
                raise ValueError("total_tokens must not be smaller than known component tokens")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    text: str
    provider_id: str
    provider_kind: ProviderKind
    model: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("provider_id", self.provider_id),
            ("model", self.model),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        if not isinstance(self.text, str):
            raise TypeError("response text must be a string")
        if not isinstance(self.provider_kind, ProviderKind):
            raise TypeError("provider_kind must be a ProviderKind")
        if not isinstance(self.usage, ModelUsage):
            raise TypeError("usage must be ModelUsage")
        if self.latency_ms is not None:
            if isinstance(self.latency_ms, bool) or not isinstance(
                self.latency_ms, (int, float)
            ):
                raise TypeError("latency_ms must be numeric")
            if not math.isfinite(float(self.latency_ms)) or self.latency_ms < 0:
                raise ValueError("latency_ms must be finite and non-negative")


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
    cost_class: ProviderCostClass | None = None
    resource_class: ProviderResourceClass | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if self.provider_id != self.provider_id.strip():
            raise ValueError("provider_id must not contain surrounding whitespace")
        if not isinstance(self.kind, ProviderKind):
            raise TypeError("kind must be a ProviderKind")
        if not isinstance(self.supports_private_data, bool):
            raise TypeError("supports_private_data must be a boolean")
        for name, value in (
            ("supports_tools", self.supports_tools),
            ("supports_streaming", self.supports_streaming),
            ("supports_hard_cancellation", self.supports_hard_cancellation),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean")
        if self.cost_class is not None and not isinstance(
            self.cost_class, ProviderCostClass
        ):
            raise TypeError("cost_class must be a ProviderCostClass")
        if self.resource_class is not None and not isinstance(
            self.resource_class, ProviderResourceClass
        ):
            raise TypeError("resource_class must be a ProviderResourceClass")


class ModelGatewayError(RuntimeError):
    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        provider_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.retryable = retryable


class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
