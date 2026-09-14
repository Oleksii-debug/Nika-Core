from __future__ import annotations

from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeResult
from nika_core.runtime.retry import RetryPolicy, usable_resume_token


class _SpoofingResumeToken(str):
    """Text-shaped value that lies about equality with persisted authority."""

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False

    __hash__ = str.__hash__


def test_usable_resume_token_requires_exact_builtin_text() -> None:
    spoofed = _SpoofingResumeToken("attacker-controlled")

    # A str subclass can override reflected equality even when the trusted value is on the left.
    assert "persisted-session-token" == spoofed
    assert usable_resume_token(spoofed) is None
    assert usable_resume_token("persisted-session-token") == "persisted-session-token"


def test_spoofed_resume_token_cannot_authorize_retry() -> None:
    result = RuntimeResult(
        outcome=RuntimeOutcome.FAILED,
        resume_token=_SpoofingResumeToken("attacker-controlled"),
        error="temporary runtime failure",
        error_code=RuntimeErrorCode.TRANSIENT,
    )
    policy = RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        allow_fresh_retry=False,
    )

    assert policy.should_retry(result, retries_used=0) is False