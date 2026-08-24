from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalAuthority,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    ReviewEvidence,
    ReviewSubject,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
    project_purpose_review_subject,
)
from nika_core.tools import ToolRisk

_NOW = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)


def _authority(secret: bytes = b"a" * 32) -> ApprovalAuthority:
    return ApprovalAuthority(issuer_id="trusted-host-test", secret=secret)


def _intent(action_id: str, *, write_bytes: int = 1) -> ActionIntent:
    return ActionIntent(
        action_id=action_id,
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target=f"exact target {action_id}",
        write_path=f"artifacts/{action_id}.txt",
        write_bytes=write_bytes,
        approval_required=True,
    )


def _policy(tmp_path: Path, authority: ApprovalAuthority, *, max_write_bytes: int = 10) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            writable_roots=("artifacts",),
        ),
        budget=ExecutionBudget(max_write_bytes=max_write_bytes),
        approval_verifier=authority.verifier(),
    )


def _approved(authority: ApprovalAuthority, intent: ActionIntent) -> ApprovalEvidence:
    request = authority.request(intent, reason="human reviewed exact action", now=_NOW)
    return authority.approve(request.request_id, now=_NOW)


def test_action_fingerprint_is_unambiguous_and_binds_approval_flag() -> None:
    separator = "\x1f"
    left = ActionIntent(
        action_id=f"a{separator}b",
        tool_id="c",
        risk=ToolRisk.LOCAL_WRITE,
        target="d",
    )
    right = ActionIntent(
        action_id="a",
        tool_id=f"b{separator}c",
        risk=ToolRisk.LOCAL_WRITE,
        target="d",
    )
    assert left.approval_fingerprint != right.approval_fingerprint
    assert replace(left, approval_required=True).approval_fingerprint != left.approval_fingerprint


def test_caller_constructed_positive_evidence_cannot_authorize(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent("forgery")
    forged = ApprovalEvidence(
        approval_id="approved=true",
        request_id="caller-request",
        issuer_id=authority.issuer_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=_NOW - timedelta(seconds=1),
        expires_at=_NOW + timedelta(minutes=1),
        signature="0" * 64,
    )
    budgets = ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10))
    approvals = ApprovalLedger()

    with pytest.raises(PermissionError, match="signature is invalid"):
        authorize_action(
            intent,
            _policy(tmp_path, authority),
            budgets,
            approvals,
            approval=forged,
            now=_NOW,
        )
    assert budgets.write_bytes == 0


def test_wrong_issuer_and_restart_secret_fail_closed(tmp_path: Path) -> None:
    old = _authority(b"a" * 32)
    intent = _intent("restart")
    evidence = _approved(old, intent)

    foreign = ApprovalAuthority(issuer_id="foreign-host", secret=b"a" * 32)
    with pytest.raises(PermissionError, match="untrusted issuer"):
        authorize_action(
            intent,
            _policy(tmp_path, foreign),
            ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
            ApprovalLedger(),
            approval=evidence,
            now=_NOW,
        )

    restarted = _authority(b"a" * 32)
    with pytest.raises(PermissionError, match="signature is invalid"):
        authorize_action(
            intent,
            _policy(tmp_path, restarted),
            ExecutionBudgetLedger(ExecutionBudget(max_write_bytes=10)),
            ApprovalLedger(),
            approval=evidence,
            now=_NOW,
        )


def test_denied_expired_and_reused_action_approvals_are_rejected(tmp_path: Path) -> None:
    authority = _authority()
    denied = authority.request(_intent("denied"), reason="review", now=_NOW)
    authority.deny(denied.request_id, now=_NOW)
    with pytest.raises(PermissionError, match="denied"):
        authority.approve(denied.request_id, now=_NOW)

    expiring = authority.request(
        _intent("expired"), reason="review", now=_NOW, ttl=timedelta(seconds=1)
    )
    with pytest.raises(KeyError, match="unknown or expired"):
        authority.approve(expiring.request_id, now=_NOW + timedelta(seconds=1))

    intent = _intent("once")
    evidence = _approved(authority, intent)
    ledger = ApprovalLedger()
    policy = _policy(tmp_path, authority)
    authorize_action(
        intent,
        policy,
        ExecutionBudgetLedger(policy.budget),
        ledger,
        approval=evidence,
        now=_NOW,
    )
    with pytest.raises(PermissionError, match="already used"):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ledger,
            approval=evidence,
            now=_NOW,
        )


def test_fresh_caller_ledger_cannot_replay_host_used_approval(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent("host-replay")
    evidence = _approved(authority, intent)
    policy = _policy(tmp_path, authority)

    authorize_action(
        intent,
        policy,
        ExecutionBudgetLedger(policy.budget),
        ApprovalLedger(),
        approval=evidence,
        now=_NOW,
    )

    with pytest.raises(PermissionError, match="trusted host"):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ApprovalLedger(),
            approval=evidence,
            now=_NOW,
        )


def test_budget_failure_does_not_burn_valid_approval(tmp_path: Path) -> None:
    authority = _authority()
    intent = _intent("budget")
    evidence = _approved(authority, intent)
    approvals = ApprovalLedger()
    denied_policy = _policy(tmp_path, authority, max_write_bytes=0)

    with pytest.raises(PermissionError, match="write budget"):
        authorize_action(
            intent,
            denied_policy,
            ExecutionBudgetLedger(denied_policy.budget),
            approvals,
            approval=evidence,
            now=_NOW,
        )

    allowed_policy = _policy(tmp_path, authority, max_write_bytes=1)
    authorize_action(
        intent,
        allowed_policy,
        ExecutionBudgetLedger(allowed_policy.budget),
        approvals,
        approval=evidence,
        now=_NOW,
    )


def test_concurrent_budget_and_approval_have_one_deterministic_commit(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority, max_write_bytes=1)
    budgets = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()
    intents = (_intent("race-a"), _intent("race-b"))
    evidence = {intent.action_id: _approved(authority, intent) for intent in intents}
    barrier = Barrier(2)

    def attempt(intent: ActionIntent) -> tuple[str, bool]:
        barrier.wait()
        try:
            authorize_action(
                intent,
                policy,
                budgets,
                approvals,
                approval=evidence[intent.action_id],
                now=_NOW,
            )
        except PermissionError:
            return intent.action_id, False
        return intent.action_id, True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, intents))

    assert sum(ok for _, ok in results) == 1
    assert budgets.write_bytes == 1
    loser = next(action_id for action_id, ok in results if not ok)
    loser_intent = next(item for item in intents if item.action_id == loser)
    authorize_action(
        loser_intent,
        policy,
        ExecutionBudgetLedger(policy.budget),
        approvals,
        approval=evidence[loser],
        now=_NOW,
    )


def test_review_subject_fingerprint_binds_project_purpose_resource_and_bindings() -> None:
    subject = ReviewSubject(
        subject_kind="pf6-production-promotion",
        project_id="project-a",
        purpose="promote-release",
        resource_id="env-production",
        bindings=(("artifact_digest", "sha256:abc"), ("release", "1.2.3")),
    )
    assert replace(subject, project_id="project-b").fingerprint != subject.fingerprint
    assert replace(subject, purpose="rollback").fingerprint != subject.fingerprint
    assert replace(subject, resource_id="env-staging").fingerprint != subject.fingerprint
    assert replace(
        subject,
        bindings=(("artifact_digest", "sha256:def"), ("release", "1.2.3")),
    ).fingerprint != subject.fingerprint


def test_review_bindings_reject_duplicate_or_noncanonical_order() -> None:
    with pytest.raises(ValueError, match="sorted"):
        ReviewSubject(
            subject_kind="pf6",
            project_id="p",
            purpose="promote",
            resource_id="r",
            bindings=(("z", "1"), ("a", "2")),
        )
    with pytest.raises(ValueError, match="unique"):
        ReviewSubject(
            subject_kind="pf6",
            project_id="p",
            purpose="promote",
            resource_id="r",
            bindings=(("a", "1"), ("a", "2")),
        )


def test_review_evidence_is_host_owned_exact_and_not_replacement_authority() -> None:
    authority = _authority()
    subject = ReviewSubject(
        subject_kind="pf6-production-promotion",
        project_id="project-a",
        purpose="promote-release",
        resource_id="production",
        bindings=(("release", "1.2.3"),),
    )
    request = authority.request_review(subject, reason="review exact release", now=_NOW)
    evidence = authority.approve_review(request.request_id, now=_NOW)

    assert authority.verify_review(subject, evidence.evidence_ref, now=_NOW) is True
    assert authority.verify_review(
        replace(subject, bindings=(("release", "1.2.4"),)),
        evidence.evidence_ref,
        now=_NOW,
    ) is False
    assert "signature" not in request.as_dict()
    assert "issuer_id" not in request.as_dict()
    assert "secret" not in request.as_dict()


def test_project_purpose_adapter_rejects_wrong_project_purpose_and_forged_ref() -> None:
    current = datetime.now(UTC)
    authority = _authority()
    subject = project_purpose_review_subject(
        subject_kind="pf10-compliance",
        project_id="project-a",
        purpose="license-disposition:component-a",
    )
    request = authority.request_review(subject, reason="legal review", now=current)
    evidence = authority.approve_review(request.request_id, now=current)
    adapter = authority.project_purpose_review_verifier(subject_kind="pf10-compliance")

    assert adapter.verify(
        project_id="project-a",
        evidence_ref=evidence.evidence_ref,
        purpose="license-disposition:component-a",
    ) is True
    assert adapter.verify(
        project_id="project-b",
        evidence_ref=evidence.evidence_ref,
        purpose="license-disposition:component-a",
    ) is False
    assert adapter.verify(
        project_id="project-a",
        evidence_ref=evidence.evidence_ref,
        purpose="license-disposition:component-b",
    ) is False
    assert adapter.verify(
        project_id="project-a",
        evidence_ref="caller-approved=true",
        purpose="license-disposition:component-a",
    ) is False


def test_forged_review_evidence_object_does_not_become_authority() -> None:
    authority = _authority()
    subject = project_purpose_review_subject(
        subject_kind="pf10-compliance", project_id="p", purpose="compliance-scope"
    )
    forged = ReviewEvidence(
        evidence_ref="review-evidence-forged",
        request_id="request-forged",
        issuer_id=authority.issuer_id,
        subject_fingerprint=subject.fingerprint,
        approved_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
        signature="0" * 64,
    )
    assert authority.verify_review(subject, forged.evidence_ref, now=_NOW) is False


def test_review_denial_expiry_and_restart_fail_closed() -> None:
    authority = _authority()
    subject = project_purpose_review_subject(
        subject_kind="pf10-compliance", project_id="p", purpose="compliance-scope"
    )
    denied = authority.request_review(subject, reason="review", now=_NOW)
    authority.deny_review(denied.request_id, now=_NOW)
    with pytest.raises(PermissionError, match="denied"):
        authority.approve_review(denied.request_id, now=_NOW)

    short = authority.request_review(
        subject, reason="review", now=_NOW, ttl=timedelta(seconds=1)
    )
    with pytest.raises(KeyError, match="unknown or expired"):
        authority.approve_review(short.request_id, now=_NOW + timedelta(seconds=1))

    request = authority.request_review(subject, reason="review", now=_NOW)
    evidence = authority.approve_review(request.request_id, now=_NOW)
    restarted = _authority(b"a" * 32)
    assert restarted.verify_review(subject, evidence.evidence_ref, now=_NOW) is False
