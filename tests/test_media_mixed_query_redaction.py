from __future__ import annotations

import pytest

from nika_core.media.privacy import redact_argv, redact_mapping, redact_text

_CANARY = "CANARY_SYNTHETIC_NOT_A_SECRET"


@pytest.mark.parametrize(
    "inner",
    [
        "https%3A%2F%2Fauth.test%2F%3Fapi_key%3D" + _CANARY + "?view=public",
        "https%253A%252F%252Fauth.test%252F%253Fapi%25255Fkey%253D"
        + _CANARY + "?view=public",
        "https://auth.test/?%3Fapi_key%3D" + _CANARY,
        "%3Fsubscription%255Fkey%3D" + _CANARY + "?note=public",
    ],
)
def test_mixed_literal_and_encoded_query_redacts_outer_value(inner: str) -> None:
    source = f"https://example.test/?next={inner}&lang=uk"
    assert redact_text(source) == "https://example.test/?next=[REDACTED]&lang=uk"


def test_mixed_encoded_query_is_redacted_in_mapping_and_argv() -> None:
    source = (
        "https://example.test/?next="
        "https%3A%2F%2Fauth.test%2F%3Fapi_key%3D"
        + _CANARY + "?view=public"
    )
    safe = "https://example.test/?next=[REDACTED]"
    assert redact_mapping({"source_url": source}) == {"source_url": safe}
    assert redact_argv(("--url", source)) == ("--url", safe)


def test_literal_nested_sensitive_key_preserves_nonsensitive_address() -> None:
    source = (
        "https://example.test/?next=https://auth.test/?api%5Fkey="
        + _CANARY + "&lang=uk"
    )
    assert redact_text(source) == (
        "https://example.test/?next=https://auth.test/?api%5Fkey="
        "[REDACTED]&lang=uk"
    )


def test_literal_nested_redirect_with_encoded_credential_is_redacted() -> None:
    source = (
        "https://example.test/?next=https://auth.test/?redirect="
        "%3Fapi_key%3D" + _CANARY + "&lang=uk"
    )
    assert redact_text(source) == (
        "https://example.test/?next=https://auth.test/?redirect="
        "[REDACTED]&lang=uk"
    )


def test_benign_encoded_query_with_literal_question_mark_stays_intact() -> None:
    source = (
        "https://example.test/?next=https%3A%2F%2Fsite.test%2F%3Fview%3Dlist"
        "?hint=uk&public_key=public-id"
    )
    assert redact_text(source) == source
