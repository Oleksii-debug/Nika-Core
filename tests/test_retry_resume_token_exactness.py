from __future__ import annotations

import pytest

from nika_core.runtime.contracts import (
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.runtime.retry import usable_resume_token


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


def test_runtime_result_rejects_spoofed_resume_token() -> None:
    with pytest.raises(TypeError, match="resume_token must be an exact string"):
        RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            resume_token=_SpoofingResumeToken("attacker-controlled"),
            error="temporary runtime failure",
        )


def test_runtime_resume_request_requires_exact_identity_strings() -> None:
    spoofed = _SpoofingResumeToken("attacker-controlled")

    for overrides in (
        {"task_id": spoofed},
        {"thread_id": spoofed},
        {"resume_token": spoofed},
    ):
        request = {
            "task_id": "task-1",
            "thread_id": "thread-1",
            "resume_token": "persisted-session-token",
            **overrides,
        }
        with pytest.raises(TypeError, match="resume identifiers must be exact strings"):
            RuntimeResumeRequest(**request)


@pytest.mark.parametrize("malformed", (b"opaque", 7))
def test_failed_result_keeps_nontext_token_for_fail_closed_or_explicit_fresh_policy(
    malformed: object,
) -> None:
    result = RuntimeResult(
        outcome=RuntimeOutcome.FAILED,
        resume_token=malformed,  # type: ignore[arg-type]
        error="temporary runtime failure",
        error_code=RuntimeErrorCode.TRANSIENT,
    )

    assert usable_resume_token(result.resume_token) is None
