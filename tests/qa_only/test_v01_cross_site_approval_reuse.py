from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec

_NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


@dataclass(slots=True)
class _EffectProbe:
    invocations: int = 0
    destinations: list[str] | None = None

    def __post_init__(self) -> None:
        if self.destinations is None:
            self.destinations = []

    def invoke(self, destination: str) -> None:
        self.invocations += 1
        assert self.destinations is not None
        self.destinations.append(destination)


def _target(*, url: str, role: str = "button", name: str = "Confirm") -> str:
    return f"browser:url={url};role={role};name={name}"


def _intent(*, action_id: str, url: str) -> ActionIntent:
    host = urlsplit(url).hostname
    assert host is not None
    return ActionIntent(
        action_id=action_id,
        tool_id="browser.effect",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target=_target(url=url),
        network_host=host,
        approval_required=True,
    )


def _approval(intent: ActionIntent, *, approval_id: str = "approval-site-a") -> ApprovalEvidence:
    return ApprovalEvidence(
        approval_id=approval_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=_NOW - timedelta(minutes=1),
        expires_at=_NOW + timedelta(minutes=4),
    )


def _policy(tmp_path: Path, *hosts: str) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"browser.effect"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            allowed_network_hosts=tuple(hosts),
        ),
        budget=ExecutionBudget(max_network_calls=4),
    )


def _attempt(
    *,
    tmp_path: Path,
    intent: ActionIntent,
    approval: ApprovalEvidence,
    probe: _EffectProbe,
    allowed_hosts: tuple[str, ...],
) -> None:
    policy = _policy(tmp_path, *allowed_hosts)
    authorize_action(
        intent,
        policy,
        ExecutionBudgetLedger(policy.budget),
        ApprovalLedger(),
        approval=approval,
        now=_NOW,
    )
    probe.invoke(intent.target)


def test_approval_for_site_a_cannot_authorize_equivalent_effect_on_site_b(tmp_path: Path) -> None:
    site_a = "https://site-a.test/account/42"
    site_b = "https://site-b.test/account/42"
    approved_intent = _intent(action_id="invoke-confirm", url=site_a)
    evidence = _approval(approved_intent)
    probe = _EffectProbe()

    with pytest.raises(PermissionError):
        _attempt(
            tmp_path=tmp_path,
            intent=_intent(action_id="invoke-confirm", url=site_b),
            approval=evidence,
            probe=probe,
            allowed_hosts=("site-a.test", "site-b.test"),
        )

    assert probe.invocations == 0
    assert probe.destinations == []


def test_resource_specific_approval_rejects_same_host_different_resource(tmp_path: Path) -> None:
    resource_a = "https://controlled.test/accounts/alpha/confirm"
    resource_b = "https://controlled.test/accounts/beta/confirm"
    approved_intent = _intent(action_id="invoke-confirm", url=resource_a)
    evidence = _approval(approved_intent)
    probe = _EffectProbe()

    with pytest.raises(PermissionError):
        _attempt(
            tmp_path=tmp_path,
            intent=_intent(action_id="invoke-confirm", url=resource_b),
            approval=evidence,
            probe=probe,
            allowed_hosts=("controlled.test",),
        )

    assert probe.invocations == 0
    assert probe.destinations == []


def test_identical_accessible_control_name_on_another_site_is_not_authority(tmp_path: Path) -> None:
    site_a = "https://site-a.test/transfer"
    site_b = "https://site-b.test/transfer"
    approved_intent = _intent(action_id="invoke-confirm", url=site_a)
    evidence = _approval(approved_intent)
    probe = _EffectProbe()

    candidate = _intent(action_id="invoke-confirm", url=site_b)
    assert approved_intent.target.endswith("role=button;name=Confirm")
    assert candidate.target.endswith("role=button;name=Confirm")

    with pytest.raises(PermissionError):
        _attempt(
            tmp_path=tmp_path,
            intent=candidate,
            approval=evidence,
            probe=probe,
            allowed_hosts=("site-a.test", "site-b.test"),
        )

    assert probe.invocations == 0
    assert probe.destinations == []


def test_redirect_to_another_origin_must_fail_before_external_handler_invocation(
    tmp_path: Path,
) -> None:
    """A trusted approval for A must not become authority for a redirect hop to B.

    The redirect map is synthetic and process-local. No network request is performed.
    This oracle intentionally attacks the seam between approval validation and an
    external adapter that may follow redirects internally.
    """

    site_a = "https://site-a.test/start"
    site_b = "https://site-b.test/finish"
    approved_intent = _intent(action_id="invoke-confirm", url=site_a)
    evidence = _approval(approved_intent, approval_id="approval-redirect-a")
    evidence_by_ref = {evidence.approval_id: evidence}
    redirect_map = {site_a: site_b}
    handler_probe = _EffectProbe()

    policy = _policy(tmp_path, "site-a.test", "site-b.test")
    budgets = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()

    async def approval_policy(_spec: ToolSpec, call: ToolCall) -> bool:
        requested_url = str(call.arguments["url"])
        approval_ref = str(call.arguments["approval_ref"])
        authorize_action(
            _intent(action_id=call.call_id, url=requested_url),
            policy,
            budgets,
            approvals,
            approval=evidence_by_ref[approval_ref],
            now=_NOW,
        )
        return True

    async def controlled_browser_handler(arguments: dict[str, object]) -> object:
        requested_url = str(arguments["url"])
        handler_probe.invoke(requested_url)
        effective_url = redirect_map.get(requested_url, requested_url)
        if effective_url != requested_url:
            handler_probe.invoke(effective_url)
        return {"effective_url": effective_url}

    executor = ToolExecutor(approval_policy=approval_policy)
    executor.register(
        ToolSpec(
            tool_id="browser.effect",
            description="synthetic controlled browser effect",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        ),
        controlled_browser_handler,
    )

    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="invoke-confirm",
                tool_id="browser.effect",
                arguments={"url": site_a, "approval_ref": evidence.approval_id},
            )
        )
    )

    assert result.ok is False
    assert handler_probe.invocations == 0
    assert handler_probe.destinations == []
