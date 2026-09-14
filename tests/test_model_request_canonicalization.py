from __future__ import annotations

import math
from dataclasses import fields
from typing import Any

import pytest

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ProviderKind,
)


def _request(**overrides: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "request_id": "request-1",
        "messages": (ModelMessage(role="user", content="hello"),),
    }
    values.update(overrides)
    return ModelRequest(**values)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("request_id", "", ValueError),
        ("request_id", "   ", ValueError),
        ("request_id", " request-1", ValueError),
        ("request_id", "request-1 ", ValueError),
        ("request_id", True, TypeError),
        ("model", "", ValueError),
        ("model", " model-a ", ValueError),
        ("model", False, TypeError),
        ("provider_id", "", ValueError),
        ("provider_id", " provider-a ", ValueError),
        ("provider_id", 1, TypeError),
    ),
)
def test_request_identifiers_reject_noncanonical_values(
    field: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _request(**{field: value})


def test_optional_model_and_provider_ids_preserve_nika_default_semantics() -> None:
    request = _request(model=None, provider_id=None, provider_kind=ProviderKind.LOCAL)

    assert request.model is None
    assert request.provider_id is None
    assert request.provider_kind is ProviderKind.LOCAL


@pytest.mark.parametrize("role", ("system", "user", "assistant", "tool"))
def test_message_roles_are_exact_nika_owned_strings(role: str) -> None:
    message = ModelMessage(role=role, content="content")

    assert message.role == role


@pytest.mark.parametrize("role", (" user", "USER", "provider", "function"))
def test_message_roles_reject_noncanonical_or_provider_specific_values(role: str) -> None:
    with pytest.raises(ValueError, match="unsupported message role"):
        ModelMessage(role=role, content="content")


def test_message_role_rejects_non_string_ambiguity() -> None:
    with pytest.raises(TypeError, match="message role must be text"):
        ModelMessage(role=True, content="content")  # type: ignore[arg-type]


@pytest.mark.parametrize("content", ("", " ", "\t\n"))
def test_message_rejects_empty_or_whitespace_only_content(content: str) -> None:
    with pytest.raises(ValueError, match="message content must not be empty"):
        ModelMessage(role="user", content=content)


def test_message_preserves_meaningful_content_whitespace() -> None:
    message = ModelMessage(role="user", content="  preserve prompt spacing  ")

    assert message.content == "  preserve prompt spacing  "


def test_message_content_rejects_non_string_values() -> None:
    with pytest.raises(TypeError, match="message content must be text"):
        ModelMessage(role="user", content=123)  # type: ignore[arg-type]


def test_request_canonicalizes_semantically_equivalent_sequence_and_number_forms() -> None:
    messages = [
        ModelMessage(role="system", content="policy"),
        ModelMessage(role="user", content="question"),
    ]
    request = _request(
        messages=messages,
        provider_id="primary",
        fallback_provider_ids=["fallback-b", "fallback-a"],
        temperature=1,
    )

    assert request.messages == tuple(messages)
    assert isinstance(request.messages, tuple)
    assert request.fallback_provider_ids == ("fallback-b", "fallback-a")
    assert isinstance(request.fallback_provider_ids, tuple)
    assert request.temperature == 1.0
    assert isinstance(request.temperature, float)


@pytest.mark.parametrize(
    "value",
    (True, False, "1", object()),
)
def test_temperature_rejects_boolean_and_non_numeric_values(value: object) -> None:
    with pytest.raises(TypeError, match="temperature must be numeric"):
        _request(temperature=value)


@pytest.mark.parametrize(
    "value",
    (-0.01, 2.01, math.nan, math.inf, -math.inf, pytest.param(10**10000, id="huge-int")),
)
def test_temperature_rejects_out_of_range_or_non_finite_values(value: object) -> None:
    with pytest.raises(ValueError, match="temperature must be finite and between 0 and 2"):
        _request(temperature=value)


@pytest.mark.parametrize("value", (None, 0, 0.25, 1, 2.0))
def test_temperature_accepts_only_canonical_domain_range(value: float | None) -> None:
    request = _request(temperature=value)

    if value is None:
        assert request.temperature is None
    else:
        assert request.temperature == float(value)
        assert isinstance(request.temperature, float)


@pytest.mark.parametrize(
    "fallbacks",
    (
        [""],
        ["   "],
        [" fallback"],
        ["fallback "],
        [True],
    ),
)
def test_fallback_provider_ids_reject_empty_whitespace_or_non_text_values(
    fallbacks: list[object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(fallback_provider_ids=fallbacks)


def test_fallback_provider_ids_reject_duplicates_and_primary_repetition() -> None:
    with pytest.raises(ValueError, match="fallback provider IDs must be unique"):
        _request(fallback_provider_ids=["fallback", "fallback"])

    with pytest.raises(ValueError, match="primary provider cannot also be a fallback"):
        _request(provider_id="primary", fallback_provider_ids=["primary"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("provider_kind", "local", "provider_kind must be a ProviderKind"),
        ("privacy", "private", "privacy must be a PrivacyClass"),
    ),
)
def test_enum_contracts_reject_raw_string_equivalents(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        _request(**{field: value})


def test_metadata_is_provider_neutral_sorted_and_immutable() -> None:
    supplied = {"zeta": "last", "alpha": "first"}
    request = _request(metadata=supplied)
    supplied["alpha"] = "mutated outside"

    assert list(request.metadata.items()) == [("alpha", "first"), ("zeta", "last")]
    with pytest.raises(TypeError):
        request.metadata["alpha"] = "mutated inside"  # type: ignore[index]


@pytest.mark.parametrize(
    "metadata",
    (
        [],
        {1: "value"},
        {"": "value"},
        {" key ": "value"},
        {"key": ""},
        {"key": "   "},
        {"key": 1},
        {"options": {"temperature": 0.5}},
    ),
)
def test_metadata_rejects_non_string_or_nested_provider_structures(metadata: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(metadata=metadata)


def test_metadata_preserves_meaningful_value_whitespace() -> None:
    request = _request(metadata={"note": "  meaningful text  "})

    assert request.metadata["note"] == "  meaningful text  "


def test_provider_specific_request_parameters_are_not_domain_fields() -> None:
    domain_fields = {item.name for item in fields(ModelRequest)}

    assert domain_fields.isdisjoint({"options", "payload", "headers", "api_key", "think", "max_tokens"})
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ModelRequest(
            request_id="request-1",
            messages=(ModelMessage(role="user", content="hello"),),
            options={"temperature": 0.5},  # type: ignore[call-arg]
        )
