from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ModelMessage, ModelRequest


class _HostileModelMessage(ModelMessage):
    forged = False

    def __getattribute__(self, name: str):
        if name in {"role", "content"} and type(self).forged:
            raise AssertionError("hostile message attribute access crossed canonical boundary")
        return super().__getattribute__(name)


def test_model_request_rejects_message_subclass_before_polymorphic_access() -> None:
    message = _HostileModelMessage(role="user", content="safe")
    _HostileModelMessage.forged = True
    try:
        with pytest.raises(TypeError, match="messages must contain only ModelMessage values"):
            ModelRequest(request_id="request-1", messages=[message])
    finally:
        _HostileModelMessage.forged = False


def test_model_request_retains_exact_messages_and_list_to_tuple_canonicalization() -> None:
    message = ModelMessage(role="user", content="safe")

    request = ModelRequest(request_id="request-1", messages=[message])

    assert request.messages == (message,)
    assert request.messages[0] is message
