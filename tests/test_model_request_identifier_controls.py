from __future__ import annotations

import pytest

from nika_core.model_gateway.contracts import ModelMessage, ModelRequest


def _request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "messages": (ModelMessage(role="user", content="hello"),),
    }
    values.update(overrides)
    return ModelRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("request_id", "request\x00a"),
        ("request_id", "request\nx"),
        ("request_id", "request\u0085x"),
        ("request_id", "request\u200bx"),
        ("model", "model\x00a"),
        ("model", "model\nx"),
        ("model", "model\u0085x"),
        ("model", "model\u200bx"),
        ("provider_id", "cloud\x00a"),
        ("provider_id", "cloud\nx"),
        ("provider_id", "cloud\u0085x"),
        ("provider_id", "cloud\u200bx"),
    ),
)
def test_request_identifiers_reject_internal_control_characters(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        _request(**{field: value})


@pytest.mark.parametrize(
    "provider_id",
    (
        "fallback\x00a",
        "fallback\nx",
        "fallback\u0085x",
        "fallback\u200bx",
    ),
)
def test_fallback_provider_ids_reject_internal_control_characters(
    provider_id: str,
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        _request(fallback_provider_ids=(provider_id,))


@pytest.mark.parametrize("key", ("trace\x00id", "trace\u0085id", "trace\u200bid"))
def test_metadata_keys_share_identifier_control_character_boundary(key: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        _request(metadata={key: "safe-value"})


def test_printable_unicode_identifier_remains_supported() -> None:
    request = _request(
        request_id="запит-一",
        model="модель-β",
        provider_id="локальний-λ",
        fallback_provider_ids=("резерв-δ",),
        metadata={"мітка-π": "safe-value"},
    )

    assert request.request_id == "запит-一"
    assert request.model == "модель-β"
    assert request.provider_id == "локальний-λ"
    assert request.fallback_provider_ids == ("резерв-δ",)
    assert request.metadata["мітка-π"] == "safe-value"


def test_message_content_keeps_separate_text_semantics() -> None:
    message = ModelMessage(role="user", content="line one\nline two")
    request = _request(messages=(message,))

    assert request.messages == (message,)
    assert request.messages[0].content == "line one\nline two"
