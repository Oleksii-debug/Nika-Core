from __future__ import annotations

from typing import Any

import pytest

from nika_core.model_gateway.contracts import ModelMessage, ModelRequest


class _HostileText(str):
    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile text strip must not run")

    def isprintable(self) -> bool:
        raise AssertionError("hostile text printability must not run")

    def __hash__(self) -> int:
        raise AssertionError("hostile text hash must not run")


class _HostileInt(int):
    def __float__(self) -> float:
        raise AssertionError("hostile int conversion must not run")


class _HostileFloat(float):
    def __float__(self) -> float:
        raise AssertionError("hostile float conversion must not run")


def _request(**overrides: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "request_id": "request-1",
        "messages": (ModelMessage(role="user", content="fixture"),),
    }
    values.update(overrides)
    return ModelRequest(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("request_id", _HostileText("request-1")),
        ("model", _HostileText("model-a")),
        ("provider_id", _HostileText("provider-a")),
    ),
)
def test_request_identifiers_reject_string_subclasses_before_hooks(
    field: str,
    value: str,
) -> None:
    with pytest.raises(TypeError, match="must be text"):
        _request(**{field: value})


def test_fallback_provider_id_rejects_string_subclass_before_hooks() -> None:
    with pytest.raises(TypeError, match="fallback provider ID must be text"):
        _request(fallback_provider_ids=[_HostileText("fallback-a")])


def test_message_role_rejects_string_subclass_before_hooks() -> None:
    with pytest.raises(TypeError, match="message role must be text"):
        ModelMessage(role=_HostileText("user"), content="fixture")


def test_message_content_rejects_string_subclass_before_hooks() -> None:
    with pytest.raises(TypeError, match="message content must be text"):
        ModelMessage(role="user", content=_HostileText("fixture"))


def test_metadata_value_rejects_string_subclass_before_hooks() -> None:
    with pytest.raises(TypeError, match="metadata values must be text"):
        _request(metadata={"source": _HostileText("fixture")})


@pytest.mark.parametrize("value", (_HostileInt(1), _HostileFloat(1.0)))
def test_temperature_rejects_numeric_subclasses_before_hooks(value: object) -> None:
    with pytest.raises(TypeError, match="temperature must be numeric"):
        _request(temperature=value)
