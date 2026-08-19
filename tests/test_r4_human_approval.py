from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalAuthority,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolRisk

_NOW = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)


def _intent(*, action_id: str = "danger-1") -> ActionIntent:
    return ActionIntent(
        action_id=action_id,
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="named destructive operation",
        write_path="artifacts/result.txt",
        write_bytes=1,
    )


def _policy(tmp_path: Path, authority: ApprovalAuthority) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            writable_roots=("artifacts",),
        ),
        budget=ExecutionBudget(max_write_bytes=10),
        approval_verifier=authority.verifier(),
    )


def test_request_view_exposes_exact_action_but_not_evidence_or_secret() -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    view = authority.request(
        _intent(),
        reason="delete exactly the reviewed artifact",
        now=_NOW,
    )
    data = view.as_dict()

    assert data["action_id"] == "danger-1"
    assert data["tool_id"] == "danger.execute"
    assert data["risk"] == ToolRisk.HIGH_IMPACT.value
    assert data["target"] == "named destructive operation"
    assert data["write_path"] == "artifacts/result.txt"
    assert data["reason"] == "delete exactly the reviewed artifact"
    assert "signature" not in data
    assert "approval_id" not in data
    assert "issuer_id" not in data
    assert "secret" not in data


def test_low_risk_action_cannot_be_laundered_through_r4_request() -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    intent = ActionIntent(
        action_id="read-1",
        tool_id="files.read",
        risk=ToolRisk.READ_ONLY,
        target="public file",
    )
    with pytest.raises(ValueError, match="reserved for approval-gated"):
        authority.request(intent, reason="unnecessary", now=_NOW)


def test_trusted_evidence_authorizes_exact_action(tmp_path: Path) -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    intent = _intent()
    request = authority.request(intent, reason="explicit test approval", now=_NOW)
    evidence = authority.approve(request.request_id, now=_NOW)

    decision = authorize_action(
        intent,
        _policy(tmp_path, authority),
        ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
        ApprovalLedger(),
        approval=evidence,
        now=_NOW,
    )
    assert decision.approved is True
    assert authority.evidence(request.request_id, now=_NOW) == evidence
    assert authority.pending_views(now=_NOW) == ()


def test_forged_signature_is_rejected_before_ledger_consumption(tmp_path: Path) -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    intent = _intent()
    request = authority.request(intent, reason="explicit test approval", now=_NOW)
    issued = authority.approve(request.request_id, now=_NOW)
    forged = ApprovalEvidence(
        approval_id=issued.approval_id,
        request_id=issued.request_id,
        issuer_id=issued.issuer_id,
        action_fingerprint=issued.action_fingerprint,
        approved_at=issued.approved_at,
        expires_at=issued.expires_at,
        signature="0" * 64,
    )
    ledger = ApprovalLedger()

    with pytest.raises(PermissionError, match="signature is invalid"):
        authorize_action(
            intent,
            _policy(tmp_path, authority),
            ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
            ledger,
            approval=forged,
            now=_NOW,
        )

    ledger.consume(intent, issued, now=_NOW)


def test_new_process_secret_rejects_old_evidence(tmp_path: Path) -> None:
    old = ApprovalAuthority(issuer_id="desktop-stable-id", secret=b"a" * 32)
    intent = _intent()
    request = old.request(intent, reason="old process approval", now=_NOW)
    evidence = old.approve(request.request_id, now=_NOW)
    restarted = ApprovalAuthority(issuer_id="desktop-stable-id", secret=b"b" * 32)

    with pytest.raises(PermissionError, match="signature is invalid"):
        authorize_action(
            intent,
            _policy(tmp_path, restarted),
            ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
            ApprovalLedger(),
            approval=evidence,
            now=_NOW,
        )


def test_wrong_issuer_is_rejected(tmp_path: Path) -> None:
    trusted = ApprovalAuthority(issuer_id="trusted-desktop", secret=b"a" * 32)
    foreign = ApprovalAuthority(issuer_id="foreign-desktop", secret=b"a" * 32)
    intent = _intent()
    request = foreign.request(intent, reason="foreign process", now=_NOW)
    evidence = foreign.approve(request.request_id, now=_NOW)

    with pytest.raises(PermissionError, match="untrusted issuer"):
        authorize_action(
            intent,
            _policy(tmp_path, trusted),
            ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
            ApprovalLedger(),
            approval=evidence,
            now=_NOW,
        )


def test_denied_request_cannot_be_approved() -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    request = authority.request(_intent(), reason="dangerous", now=_NOW)
    authority.deny(request.request_id, now=_NOW)

    assert authority.pending_views(now=_NOW) == ()
    with pytest.raises(PermissionError, match="denied"):
        authority.approve(request.request_id, now=_NOW)


def test_expired_request_disappears_and_cannot_issue_evidence() -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    request = authority.request(
        _intent(),
        reason="short lived",
        now=_NOW,
        ttl=timedelta(seconds=1),
    )
    later = _NOW + timedelta(seconds=1)

    assert authority.pending_views(now=later) == ()
    with pytest.raises(KeyError, match="unknown or expired"):
        authority.approve(request.request_id, now=later)


def test_request_ttl_is_bounded() -> None:
    authority = ApprovalAuthority(issuer_id="desktop-test", secret=b"k" * 32)
    with pytest.raises(ValueError, match="at most 15 minutes"):
        authority.request(
            _intent(),
            reason="too long",
            now=_NOW,
            ttl=timedelta(minutes=16),
        )
