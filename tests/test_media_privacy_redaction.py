from __future__ import annotations

import pytest

from nika_core.media.privacy import redact_argv, redact_mapping, redact_text

_CANARY = "CANARY_NOT_A_REAL_SECRET"


@pytest.mark.parametrize(
    "key",
    (
        "subscription-key",
        "subscription_key",
        "subscriptionKey",
        "api%5Fkey",
        "api%2Dkey",
        "x-api-key",
        "x%2Dapi%2Dkey",
        "client%5Fsecret",
        "access%5Ftoken",
        "signature",
        "sig",
        "expires",
    ),
)
def test_signed_and_encoded_query_aliases_do_not_leak(key: str) -> None:
    result = redact_text(f"https://example.test/file?{key}={_CANARY}&page=2")
    assert _CANARY not in result
    assert f"{key}=[REDACTED]" in result
    assert "page=2" in result


def test_sensitive_header_assignment_and_nested_mapping_are_redacted() -> None:
    assert _CANARY not in redact_text(f"subscription-key: {_CANARY}")
    payload = {
        "subscription-key": _CANARY,
        "api%5fkey": _CANARY,
        "xApiKey": _CANARY,
        "nested": {"subscriptionKey": _CANARY},
        "subscription": "public-catalog",
        "public_key": "public-id",
    }
    result = redact_mapping(payload)
    for key in ("subscription-key", "api%5fkey", "xApiKey"):
        assert result[key] == "[REDACTED]"
    assert result["nested"] == {"subscriptionKey": "[REDACTED]"}
    assert result["subscription"] == "public-catalog"
    assert result["public_key"] == "public-id"


@pytest.mark.parametrize(
    "option",
    (
        "--subscription-key",
        "--subscription_key",
        "--subscriptionKey",
        "--x-api-key",
        "--x_api_key",
    ),
)
def test_sensitive_argv_options_redact_following_value_and_equals(option: str) -> None:
    assert redact_argv((option, _CANARY, "--verbose")) == (
        option,
        "[REDACTED]",
        "--verbose",
    )
    assert redact_argv((f"{option}={_CANARY}",)) == (f"{option}=[REDACTED]",)


def test_benign_query_parameters_remain_readable() -> None:
    source = "https://example.test/catalog?subscription=public-catalog&public_key=public-id&page=2"
    assert redact_text(source) == source
