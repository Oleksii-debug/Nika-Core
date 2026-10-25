"""Reject escaped invalid Unicode in persisted Windows settings JSON."""

from __future__ import annotations

import pytest

from nika_core.v01_model_settings import ModelSelection, ModelSetupError
from nika_core.v01_settings_json import load_persisted_json_object


@pytest.mark.parametrize(
    "body",
    [
        '{"ignored":{"value":"\\ud800"}}',
        '{"\\ud800":"value"}',
        '{"ignored":["ok","\\udfff"]}',
    ],
)
def test_escaped_surrogate_is_rejected_anywhere_in_stored_json(body: str) -> None:
    with pytest.raises(ValueError, match="invalid Unicode"):
        load_persisted_json_object(body, max_bytes=4096)


def test_escaped_surrogate_model_fails_with_existing_safe_error() -> None:
    body = (
        '{"route_kind":"ollama","provider_id":"ollama",'
        '"model":"\\ud800","base_url":"http://localhost:11434"}'
    )
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        ModelSelection.from_stored(body)


def test_valid_supplementary_unicode_remains_accepted() -> None:
    body = '{"label":"😀","nested":{"text":"𝄞"}}'
    assert load_persisted_json_object(body, max_bytes=4096) == {
        "label": "😀",
        "nested": {"text": "𝄞"},
    }
