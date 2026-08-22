from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.security import ActionIntent, ApprovalEvidence, ApprovalLedger
from nika_core.tools import ToolRisk

_SEPARATOR = "\x1f"
_NOW = datetime(2026, 8, 19, 17, 0, tzinfo=UTC)


def _legacy_payload(intent: ActionIntent) -> str:
    return _SEPARATOR.join(
        (
            intent.action_id,
            intent.tool_id,
            intent.risk.value,
            intent.target,
            intent.write_path or "",
            str(intent.write_bytes),
            intent.network_host or "",
            intent.executable or "",
        )
    )


def test_embedded_legacy_separator_no_longer_aliases_field_boundaries() -> None:
    left = ActionIntent(
        action_id="a",
        tool_id=f"b{_SEPARATOR}c",
        risk=ToolRisk.READ_ONLY,
        target="target",
    )
    right = ActionIntent(
        action_id=f"a{_SEPARATOR}b",
        tool_id="c",
        risk=ToolRisk.READ_ONLY,
        target="target",
    )

    assert _legacy_payload(left) == _legacy_payload(right)
    assert left.approval_fingerprint != right.approval_fingerprint


def test_absent_and_explicit_empty_optional_value_have_distinct_identity() -> None:
    absent = ActionIntent(
        action_id="network-shape",
        tool_id="browser.read",
        risk=ToolRisk.READ_ONLY,
        target="page",
        network_host=None,
    )
    empty = ActionIntent(
        action_id="network-shape",
        tool_id="browser.read",
        risk=ToolRisk.READ_ONLY,
        target="page",
        network_host="",
    )

    assert _legacy_payload(absent) == _legacy_payload(empty)
    assert absent.approval_fingerprint != empty.approval_fingerprint


def test_approval_required_flag_participates_in_exact_identity() -> None:
    optional = ActionIntent(
        action_id="approval-mode",
        tool_id="files.write",
        risk=ToolRisk.LOCAL_WRITE,
        target="artifact",
        approval_required=False,
    )
    required = ActionIntent(
        action_id="approval-mode",
        tool_id="files.write",
        risk=ToolRisk.LOCAL_WRITE,
        target="artifact",
        approval_required=True,
    )

    assert _legacy_payload(optional) == _legacy_payload(required)
    assert optional.approval_fingerprint != required.approval_fingerprint


def test_evidence_for_formerly_colliding_action_is_rejected_for_other_action() -> None:
    approved = ActionIntent(
        action_id="a",
        tool_id=f"b{_SEPARATOR}c",
        risk=ToolRisk.HIGH_IMPACT,
        target="named operation",
    )
    other = ActionIntent(
        action_id=f"a{_SEPARATOR}b",
        tool_id="c",
        risk=ToolRisk.HIGH_IMPACT,
        target="named operation",
    )
    evidence = ApprovalEvidence(
        approval_id="approval-framing",
        action_fingerprint=approved.approval_fingerprint,
        approved_at=_NOW - timedelta(minutes=1),
        expires_at=_NOW + timedelta(minutes=4),
    )

    with pytest.raises(PermissionError, match="exact action"):
        ApprovalLedger().consume(other, evidence, now=_NOW)


def test_unicode_and_control_characters_remain_deterministic_exact_input() -> None:
    first = ActionIntent(
        action_id="дія\n1",
        tool_id="tool.✓",
        risk=ToolRisk.READ_ONLY,
        target="ціль\tα",
    )
    second = ActionIntent(
        action_id="дія\n1",
        tool_id="tool.✓",
        risk=ToolRisk.READ_ONLY,
        target="ціль\tα",
    )
    changed = ActionIntent(
        action_id="дія\n1",
        tool_id="tool.✓",
        risk=ToolRisk.READ_ONLY,
        target="ціль α",
    )

    assert first.approval_fingerprint == second.approval_fingerprint
    assert first.approval_fingerprint != changed.approval_fingerprint


def test_lone_surrogate_is_ascii_escaped_before_hashing() -> None:
    surrogate = ActionIntent(
        action_id="surrogate",
        tool_id="tool.read",
        risk=ToolRisk.READ_ONLY,
        target="target-\ud800",
    )
    changed = ActionIntent(
        action_id="surrogate",
        tool_id="tool.read",
        risk=ToolRisk.READ_ONLY,
        target="target-\ud801",
    )

    assert len(surrogate.approval_fingerprint) == 64
    assert surrogate.approval_fingerprint != changed.approval_fingerprint
