"""Plan 1 Section 2: untrusted checkpoint diagnostics never become forged audit lines."""

from __future__ import annotations

import pytest

from nika_core.runtime.contracts import (
    MAX_RUNTIME_PROBE_REASON_UTF8_BYTES,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)


class HostileReason(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("untrusted str subclass behavior executed")


@pytest.mark.parametrize(
    "reason",
    [
        False,
        123,
        HostileReason("looks trustworthy"),
    ],
)
def test_probe_rejects_nonplain_text_before_polymorphic_operations(reason: object) -> None:
    with pytest.raises(TypeError, match="plain string"):
        RuntimeResumeProbe(status=RuntimeResumeProbeStatus.MISSING, reason=reason)


@pytest.mark.parametrize(
    "reason",
    [
        "",
        " ",
        "trusted ",
        "line one\nREADY checkpoint=spoof",
        "line one\rFORGED",
        "ready\u202enot-ready",
        "ready\u200bhidden",
        "ready\u2028forged",
        "ready\u0085forged",
        "e\u0301",
        "\ud800",
    ],
)
def test_probe_rejects_ambiguous_malformed_and_injected_diagnostics(reason: str) -> None:
    with pytest.raises(ValueError, match="resume probe reason"):
        RuntimeResumeProbe(status=RuntimeResumeProbeStatus.UNVERIFIABLE, reason=reason)


def test_probe_reason_bounds_utf8_bytes_not_only_code_points() -> None:
    assert MAX_RUNTIME_PROBE_REASON_UTF8_BYTES == 2048
    good = "✓" * 682  # 2046 UTF-8 bytes
    assert RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.MISSING, reason=good
    ).reason == good
    with pytest.raises(ValueError, match="size limit"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.MISSING, reason="✓" * 683
        )
    with pytest.raises(ValueError, match="size limit"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.MISSING, reason="x" * 2049
        )


def test_valid_probe_status_and_checkpoint_authority_unchanged() -> None:
    ready = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.READY,
        reason="persisted LangGraph checkpoint is readable",
        checkpoint_id="cursor:42",
    )
    assert ready.can_resume is True
    assert ready.checkpoint_id == "cursor:42"
    missing = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.MISSING,
        reason="checkpoint lookup failed",
    )
    assert missing.can_resume is False
    with pytest.raises(ValueError, match="checkpoint_id"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="checkpoint lookup succeeded",
        )
