from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ModelMessage, ModelRequest


class _HostileModelMessage(ModelMessage):
    forged = False

    def __getattribute__(self, name: str):
        if name in {"role", "content"} and type(self).forged:
            raise AssertionError("hostile message attribute access crossed canonical boundary")
        return super().__getattribute__(name)


class _HostileText(str):
    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile text strip must not cross canonical boundary")


def _forge_exact_message(*, role: object, content: object) -> ModelMessage:
    message = object.__new__(ModelMessage)
    object.__setattr__(message, "role", role)
    object.__setattr__(message, "content", content)
    return message


def test_model_request_rejects_message_subclass_before_polymorphic_access() -> None:
    message = _HostileModelMessage(role="user", content="safe")
    _HostileModelMessage.forged = True
    try:
        with pytest.raises(TypeError, match="messages must contain only ModelMessage values"):
            ModelRequest(request_id="request-1", messages=[message])
    finally:
        _HostileModelMessage.forged = False


@pytest.mark.parametrize(
    ("role", "content", "error"),
    (
        ("developer", "safe", ValueError),
        ("user", "   ", ValueError),
        (_HostileText("user"), "safe", TypeError),
        ("user", _HostileText("safe"), TypeError),
    ),
)
def test_model_request_revalidates_exact_constructor_bypass_messages(
    role: object,
    content: object,
    error: type[Exception],
) -> None:
    message = _forge_exact_message(role=role, content=content)

    with pytest.raises(error):
        ModelRequest(request_id="request-1", messages=[message])


def test_model_request_reconstructs_exact_messages_and_list_to_tuple() -> None:
    message = ModelMessage(role="user", content="safe")

    request = ModelRequest(request_id="request-1", messages=[message])

    assert request.messages == (message,)
    assert isinstance(request.messages, tuple)
    assert request.messages[0] is not message
