from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.security import (
    ApprovalAuthority,
    ApprovalLedger,
    ExecutionBudgetLedger,
    authorize_action,
)
from nika_core.tools import ToolRisk
from nika_core.toolsmith import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
    RepositorySnapshot,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.workspaces import (
    CapabilityToolBinding,
    DownstreamBudgetLimits,
    build_toolsmith_security_envelope,
)


def _job(
    tmp_path: Path,
    *,
    isolation: IsolationClass = IsolationClass.POLICY_ONLY,
    network: NetworkPolicy | None = None,
    process: ProcessPolicy | None = None,
) -> CodingJob:
    return CodingJob(
        job_id="job-bridge",
        task_id="task-bridge",
        goal="verify a bounded capability candidate",
        repository=RepositorySnapshot(
            repository_id="Oleksii-debug/Nika-Core",
            base_sha="a" * 40,
            tree_digest="sha256:tree",
        ),
        lease=WorkspaceLease(
            lease_id="lease-bridge",
            workspace_root=tmp_path / "worker",
            isolation_class=isolation,
            expires_at="2026-08-20T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(("src/nika_core/workspaces", "tests")),
        process_policy=process or ProcessPolicy(("python", "ruff")),
        network_policy=network or NetworkPolicy(),
        resource_budget=ResourceBudget(
            timeout_seconds=120,
            max_output_bytes=100_000,
            max_changed_files=8,
        ),
        acceptance_commands=(
            AcceptanceCommand(
                ("python", "-m", "pytest", "tests/test_workspace_toolsmith_bridge.py")
            ),
        ),
        permission_ceiling=frozenset(
            {"workspace.write", "tests.run", "network.approved", "release.high_impact"}
        ),
    )


def _budget() -> DownstreamBudgetLimits:
    return DownstreamBudgetLimits(
        max_write_bytes=1024,
        max_network_calls=2,
        max_process_launches=2,
    )


def _bindings() -> tuple[CapabilityToolBinding, ...]:
    return (
        CapabilityToolBinding(
            permission="workspace.write",
            tool_id="files.write",
            max_risk=ToolRisk.LOCAL_WRITE,
        ),
        CapabilityToolBinding(
            permission="tests.run",
            tool_id="process.test",
            max_risk=ToolRisk.LOCAL_WRITE,
        ),
    )


def _authority() -> ApprovalAuthority:
    return ApprovalAuthority(issuer_id="toolsmith-bridge-test", secret=b"t" * 32)


def test_bridge_reuses_canonical_toolsmith_paths_and_m10_policy(tmp_path: Path) -> None:
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path),
        bindings=_bindings(),
        budget=_budget(),
    )
    assert envelope.untrusted_execution_ready is False
    assert envelope.tool_id_for("tests.run") == "process.test"
    assert envelope.resolve_write("src/nika_core/workspaces/new.py") == (
        tmp_path / "worker" / "src" / "nika_core" / "workspaces" / "new.py"
    ).resolve()
    assert envelope.security_policy.sandbox.allowed_network_hosts == ()
    assert set(envelope.security_policy.sandbox.allowed_executables) == {"python", "ruff"}


@pytest.mark.parametrize(
    "path",
    (
        "../outside.py",
        ".git/config",
        "src/nika_core/workspaces/file.py:stream",
        "C:\\outside\\file.py",
        "/absolute/file.py",
    ),
)
def test_bridge_preserves_toolsmith_stricter_path_rejections(
    tmp_path: Path, path: str
) -> None:
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path),
        bindings=_bindings(),
        budget=_budget(),
    )
    with pytest.raises(PermissionError):
        envelope.resolve_write(path)


def test_bridge_cannot_grant_permission_outside_toolsmith_ceiling(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="outside job permission ceiling"):
        build_toolsmith_security_envelope(
            _job(tmp_path),
            bindings=(
                CapabilityToolBinding(
                    permission="network.any",
                    tool_id="browser.anywhere",
                    max_risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
                ),
            ),
            budget=_budget(),
        )


def test_binding_risk_ceiling_prevents_relabelled_escalation(tmp_path: Path) -> None:
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path),
        bindings=_bindings(),
        budget=_budget(),
    )
    with pytest.raises(PermissionError, match="risk exceeds"):
        envelope.intent(
            permission="tests.run",
            action_id="test-as-danger",
            risk=ToolRisk.HIGH_IMPACT,
            target="release",
            executable="python",
        )


@pytest.mark.parametrize(
    "isolation",
    (IsolationClass.POLICY_ONLY, IsolationClass.PROCESS_CONTAINED),
)
def test_policy_or_process_containment_cannot_claim_untrusted_sandbox(
    tmp_path: Path, isolation: IsolationClass
) -> None:
    with pytest.raises(PermissionError, match="OS_SANDBOXED or REMOTE_SANDBOXED"):
        build_toolsmith_security_envelope(
            _job(tmp_path, isolation=isolation),
            bindings=_bindings(),
            budget=_budget(),
            require_untrusted_execution=True,
        )


def test_os_sandboxed_lease_can_form_untrusted_execution_envelope(tmp_path: Path) -> None:
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path, isolation=IsolationClass.OS_SANDBOXED),
        bindings=_bindings(),
        budget=_budget(),
        require_untrusted_execution=True,
    )
    assert envelope.untrusted_execution_ready is True


def test_bridge_does_not_widen_exact_executable_path_to_basename(tmp_path: Path) -> None:
    envelope = build_toolsmith_security_envelope(
        _job(
            tmp_path,
            isolation=IsolationClass.OS_SANDBOXED,
            process=ProcessPolicy(("C:\\trusted\\python.exe",)),
        ),
        bindings=_bindings(),
        budget=_budget(),
        require_untrusted_execution=True,
    )
    envelope.intent(
        permission="tests.run",
        action_id="trusted-python",
        risk=ToolRisk.LOCAL_WRITE,
        target="tests",
        executable="C:\\TRUSTED\\python.exe",
    )
    with pytest.raises(PermissionError, match="exact Toolsmith process policy"):
        envelope.intent(
            permission="tests.run",
            action_id="other-python",
            risk=ToolRisk.LOCAL_WRITE,
            target="tests",
            executable="C:\\other\\python.exe",
        )


def test_network_allowlist_and_budget_are_enforced_by_m10(tmp_path: Path) -> None:
    job = _job(
        tmp_path,
        isolation=IsolationClass.OS_SANDBOXED,
        network=NetworkPolicy(
            mode=NetworkMode.APPROVED_HOSTS,
            approved_hosts=("packages.example.test",),
        ),
    )
    bindings = _bindings() + (
        CapabilityToolBinding(
            permission="network.approved",
            tool_id="browser.read",
            max_risk=ToolRisk.READ_ONLY,
        ),
    )
    envelope = build_toolsmith_security_envelope(
        job,
        bindings=bindings,
        budget=DownstreamBudgetLimits(
            max_write_bytes=0,
            max_network_calls=1,
            max_process_launches=0,
        ),
        require_untrusted_execution=True,
    )
    intent = envelope.intent(
        permission="network.approved",
        action_id="network-1",
        risk=ToolRisk.READ_ONLY,
        target="package metadata",
        network_host="packages.example.test",
    )
    ledger = ExecutionBudgetLedger(envelope.security_policy.budget)
    authorize_action(intent, envelope.security_policy, ledger, ApprovalLedger())
    assert ledger.network_calls == 1

    with pytest.raises(PermissionError, match="network host"):
        authorize_action(
            envelope.intent(
                permission="network.approved",
                action_id="network-2",
                risk=ToolRisk.READ_ONLY,
                target="unknown host",
                network_host="evil.example",
            ),
            envelope.security_policy,
            ExecutionBudgetLedger(envelope.security_policy.budget),
            ApprovalLedger(),
        )


def test_external_side_effect_binding_cannot_bypass_approval(tmp_path: Path) -> None:
    authority = _authority()
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path, isolation=IsolationClass.OS_SANDBOXED),
        bindings=(
            CapabilityToolBinding(
                permission="network.approved",
                tool_id="external.publish",
                max_risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            ),
        ),
        budget=_budget(),
        require_untrusted_execution=True,
        approval_verifier=authority.verifier(),
    )
    intent = envelope.intent(
        permission="network.approved",
        action_id="external-1",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        target="external service",
    )
    assert intent.approval_required is True
    with pytest.raises(PermissionError, match="explicit approval"):
        authorize_action(
            intent,
            envelope.security_policy,
            ExecutionBudgetLedger(envelope.security_policy.budget),
            ApprovalLedger(),
        )


def test_high_impact_binding_still_requires_trusted_exact_m10_approval(
    tmp_path: Path,
) -> None:
    authority = _authority()
    envelope = build_toolsmith_security_envelope(
        _job(tmp_path, isolation=IsolationClass.OS_SANDBOXED),
        bindings=(
            CapabilityToolBinding(
                permission="release.high_impact",
                tool_id="release.publish",
                max_risk=ToolRisk.HIGH_IMPACT,
            ),
        ),
        budget=_budget(),
        require_untrusted_execution=True,
        approval_verifier=authority.verifier(),
    )
    intent = envelope.intent(
        permission="release.high_impact",
        action_id="release-1",
        risk=ToolRisk.HIGH_IMPACT,
        target="named release",
    )
    now = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)
    with pytest.raises(PermissionError, match="explicit approval"):
        authorize_action(
            intent,
            envelope.security_policy,
            ExecutionBudgetLedger(envelope.security_policy.budget),
            ApprovalLedger(),
            now=now,
        )

    request = authority.request(intent, reason="publish exact named release", now=now)
    approval = authority.approve(request.request_id, now=now)
    result = authorize_action(
        intent,
        envelope.security_policy,
        ExecutionBudgetLedger(envelope.security_policy.budget),
        ApprovalLedger(),
        approval=approval,
        now=now,
    )
    assert result.approved is True
