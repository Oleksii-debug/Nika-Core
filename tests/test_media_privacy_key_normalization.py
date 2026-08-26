from __future__ import annotations

import pytest

from nika_core.media.privacy import redact_argv, redact_mapping, redact_text


SYNTHETIC_SECRET = "NIKA_SYNTHETIC_MEDIA_SECRET_7f43"


@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "clientCredential",
        "subscriptionKey",
        "subscription-key",
        "xApiKey",
        "x-api-key",
        "vendorApiKey",
        "awsAccessKeyId",
        "awsaccesskeyid",
        "googleAccessId",
        "googleaccessid",
        "xAmzCredential",
        "x-amz-credential",
        "xGoogCredential",
        "x-goog-credential",
    ],
)
def test_redact_mapping_rejects_credential_key_spelling_drift(key: str) -> None:
    redacted = redact_mapping({key: SYNTHETIC_SECRET, "mode": "safe"})

    assert redacted[key] == "[REDACTED]"
    assert redacted["mode"] == "safe"
    assert SYNTHETIC_SECRET not in repr(redacted)


@pytest.mark.parametrize(
    "text",
    [
        f"credential={SYNTHETIC_SECRET}",
        f"clientCredential={SYNTHETIC_SECRET}",
        f"subscriptionKey={SYNTHETIC_SECRET}",
        f"xApiKey:{SYNTHETIC_SECRET}",
        f"awsAccessKeyId={SYNTHETIC_SECRET}",
        f"googleAccessId={SYNTHETIC_SECRET}",
        f"xAmzCredential={SYNTHETIC_SECRET}",
        f"xGoogCredential={SYNTHETIC_SECRET}",
        f"https://example.test/watch?subscriptionKey={SYNTHETIC_SECRET}&mode=safe",
        f"https://example.test/watch?x-api-key={SYNTHETIC_SECRET}&mode=safe",
    ],
)
def test_redact_text_removes_credential_key_spelling_drift(text: str) -> None:
    redacted = redact_text(text)

    assert SYNTHETIC_SECRET not in redacted
    assert "[REDACTED]" in redacted


def test_redact_argv_applies_same_text_redaction_to_public_evidence() -> None:
    redacted = redact_argv(
        (
            "yt-dlp",
            f"https://example.test/watch?subscriptionKey={SYNTHETIC_SECRET}",
            f"xApiKey={SYNTHETIC_SECRET}",
        )
    )

    assert SYNTHETIC_SECRET not in repr(redacted)
    assert all("[REDACTED]" in part for part in redacted[1:])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tokenCount", 3),
        ("credentialCount", 4),
        ("primaryKey", "row-17"),
        ("keyboardLayout", "uk-UA"),
        ("monkey", "banana"),
    ],
)
def test_redact_mapping_preserves_non_secret_key_lookalikes(key: str, value: object) -> None:
    redacted = redact_mapping({key: value})

    assert redacted[key] == value
