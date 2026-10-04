from __future__ import annotations

import pytest

from nika_core.media.privacy import redact_argv, redact_mapping, redact_text

_CANARY = "CANARY_NOT_A_REAL_SECRET"


@pytest.mark.parametrize(
    "key",
    ("api%255Fkey", "api%25255Fkey", "subscription%255Fkey", "x%252Dapi%252Dkey"),
)
def test_multiply_encoded_query_names_are_redacted(key: str) -> None:
    text = f"https://example.test/file?{key}={_CANARY}&page=2"
    assert redact_text(text) == f"https://example.test/file?{key}=[REDACTED]&page=2"


@pytest.mark.parametrize(
    "inner",
    (
        "https%3A%2F%2Fauth.test%2F%3Fapi_key%3D" + _CANARY,
        "https%253A%252F%252Fauth.test%252F%253Fapi%25255Fkey%253D" + _CANARY,
        "%3Fsubscription%255Fkey%3D" + _CANARY,
    ),
)
def test_encoded_nested_credential_redacts_outer_value(inner: str) -> None:
    source = f"https://example.test/?next={inner}&lang=uk"
    assert redact_text(source) == "https://example.test/?next=[REDACTED]&lang=uk"


def test_encoded_nested_credential_is_redacted_in_argv_and_mapping() -> None:
    source = "https://example.test/?next=%3Fapi_key%3D" + _CANARY
    assert redact_argv(("--url", source)) == (
        "--url",
        "https://example.test/?next=[REDACTED]",
    )
    assert redact_mapping({"source_url": source}) == {
        "source_url": "https://example.test/?next=[REDACTED]",
    }
    assert redact_mapping({"api%255Fkey": _CANARY}) == {
        "api%255Fkey": "[REDACTED]",
    }


def test_benign_encoded_nested_url_and_public_key_are_preserved() -> None:
    source = (
        "https://example.test/?next=https%3A%2F%2Fsite.test%2F%3Fview%3Dlist"
        "&public_key=public-id&lang=uk"
    )
    assert redact_text(source) == source


def test_plain_nested_url_still_preserves_non_secret_address() -> None:
    source = f"https://example.test/?next=https://auth.test/?api%5Fkey={_CANARY}&lang=uk"
    assert redact_text(source) == (
        "https://example.test/?next=https://auth.test/?api%5Fkey=[REDACTED]&lang=uk"
    )
