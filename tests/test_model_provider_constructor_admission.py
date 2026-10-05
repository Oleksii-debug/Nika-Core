from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ProviderKind
from nika_core.model_gateway.providers import (
    DeterministicMockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
)


class _BehavioralText(str):
    events: list[str]

    def strip(self, *args: object, **kwargs: object) -> str:
        self.events.append("strip")
        return super().strip(*args, **kwargs)

    def rstrip(self, *args: object, **kwargs: object) -> str:
        self.events.append("rstrip")
        return super().rstrip(*args, **kwargs)

    def lower(self) -> str:
        self.events.append("lower")
        return super().lower()

    def __bool__(self) -> bool:
        self.events.append("bool")
        return len(self) != 0

    def __format__(self, format_spec: str) -> str:
        self.events.append("format")
        return super().__format__(format_spec)


def _behavioral(value: str, events: list[str]) -> _BehavioralText:
    text = _BehavioralText(value)
    text.events = events
    return text


@pytest.mark.parametrize("field", ("provider_id", "prefix"))
def test_deterministic_provider_rejects_behavioral_text_carriers(field: str) -> None:
    events: list[str] = []
    kwargs = {
        "provider_id": "deterministic",
        "prefix": "stable",
    }
    kwargs[field] = _behavioral(kwargs[field], events)

    with pytest.raises(TypeError, match="exact text"):
        DeterministicMockProvider(**kwargs)

    assert events == []


@pytest.mark.parametrize(
    "field,value",
    (
        ("provider_id", "remote"),
        ("base_url", "https://provider.invalid/v1"),
        ("default_model", "model-a"),
        ("api_key", "synthetic-key"),
    ),
)
def test_openai_provider_rejects_behavioral_text_carriers(
    field: str,
    value: str,
) -> None:
    events: list[str] = []
    kwargs: dict[str, object] = {
        "provider_id": "remote",
        "base_url": "https://provider.invalid/v1",
        "kind": ProviderKind.CLOUD,
        "default_model": "model-a",
        "api_key": None,
    }
    kwargs[field] = _behavioral(value, events)

    with pytest.raises(TypeError, match="exact text"):
        OpenAICompatibleProvider(**kwargs)  # type: ignore[arg-type]

    assert events == []


@pytest.mark.parametrize("flag", ("supports_private_data", "supports_hard_cancellation"))
def test_openai_provider_requires_exact_boolean_flags(flag: str) -> None:
    kwargs: dict[str, object] = {
        "provider_id": "remote",
        "base_url": "https://provider.invalid/v1",
        "kind": ProviderKind.CLOUD,
        "default_model": "model-a",
        flag: 1,
    }

    with pytest.raises(TypeError, match="exact boolean"):
        OpenAICompatibleProvider(**kwargs)  # type: ignore[arg-type]


def test_openai_provider_requires_exact_provider_kind() -> None:
    with pytest.raises(TypeError, match="ProviderKind"):
        OpenAICompatibleProvider(
            provider_id="remote",
            base_url="https://provider.invalid/v1",
            kind="cloud",  # type: ignore[arg-type]
            default_model="model-a",
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("default_model", "qwen3:8b"),
        ("base_url", "http://localhost:11434"),
        ("think", "medium"),
    ),
)
def test_ollama_provider_rejects_behavioral_text_before_string_hooks(
    field: str,
    value: str,
) -> None:
    events: list[str] = []
    kwargs: dict[str, object] = {
        "default_model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "think": False,
    }
    kwargs[field] = _behavioral(value, events)

    with pytest.raises(TypeError):
        OllamaProvider(**kwargs)  # type: ignore[arg-type]

    assert events == []


@pytest.mark.parametrize(
    "kwargs",
    (
        {"default_model": 7},
        {"default_model": "qwen3:8b", "base_url": 7},
        {"default_model": "qwen3:8b", "think": 7},
    ),
)
def test_ollama_provider_rejects_nontext_carriers_with_type_error(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(TypeError):
        OllamaProvider(**kwargs)  # type: ignore[arg-type]


def test_exact_supported_constructor_values_remain_accepted() -> None:
    DeterministicMockProvider(provider_id="deterministic", prefix="")
    OpenAICompatibleProvider(
        provider_id="remote",
        base_url="https://provider.invalid/v1/",
        kind=ProviderKind.CLOUD,
        default_model="model-a",
        api_key="",
        supports_private_data=False,
        supports_hard_cancellation=True,
    )
    OllamaProvider(
        default_model="qwen3:8b",
        base_url="http://localhost:11434/",
        think=" LOW ",
    )
