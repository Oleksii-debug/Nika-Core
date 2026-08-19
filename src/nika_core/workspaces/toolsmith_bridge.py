from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nika_core.security import ActionIntent, ExecutionBudget, SandboxPolicy, SecurityPolicy
from nika_core.tools import ToolRisk
from nika_core.toolsmith import AllowedPathPolicy, CodingJob, IsolationClass, NetworkMode

_ISOLATION_RANK = {
    IsolationClass.POLICY_ONLY: 0,
    IsolationClass.PROCESS_CONTAINED: 1,
    IsolationClass.OS_SANDBOXED: 2,
    IsolationClass.REMOTE_SANDBOXED: 3,
}

_RISK_RANK = {
    ToolRisk.READ_ONLY: 0,
    ToolRisk.LOCAL_WRITE: 1,
    ToolRisk.EXTERNAL_SIDE_EFFECT: 2,
    ToolRisk.HIGH_IMPACT: 3,
}


@dataclass(frozen=True, slots=True)
class CapabilityToolBinding:
    """Bind one pre-existing Toolsmith permission to one downstream Nika tool."""

    permission: str
    tool_id: str
    max_risk: ToolRisk = ToolRisk.LOCAL_WRITE

    def __post_init__(self) -> None:
        if not self.permission.strip() or not self.tool_id.strip():
            raise ValueError("permission and tool_id must not be empty")


@dataclass(frozen=True, slots=True)
class DownstreamBudgetLimits:
    """Explicit M10 ceilings; Toolsmith resource limits are not silently reinterpreted."""

    max_write_bytes: int
    max_network_calls: int
    max_process_launches: int

    def __post_init__(self) -> None:
        if min(self.max_write_bytes, self.max_network_calls, self.max_process_launches) < 0:
            raise ValueError("downstream budget limits must be non-negative")


@dataclass(frozen=True, slots=True)
class ToolsmithSecurityEnvelope:
    """M9/M10 adapter around the canonical durable Toolsmith CodingJob contract."""

    job_id: str
    workspace_root: Path
    isolation_class: IsolationClass
    allowed_paths: AllowedPathPolicy
    bindings: tuple[CapabilityToolBinding, ...]
    security_policy: SecurityPolicy
    untrusted_execution_ready: bool

    def tool_id_for(self, permission: str) -> str:
        for binding in self.bindings:
            if binding.permission == permission:
                return binding.tool_id
        raise PermissionError("permission has no downstream tool binding")

    def resolve_write(self, relative_path: str) -> Path:
        """Apply the stricter Toolsmith path policy before the M10 sandbox policy."""
        try:
            allowed = self.allowed_paths.allows(relative_path)
        except ValueError as exc:
            raise PermissionError("write path violates canonical Toolsmith path policy") from exc
        if not allowed:
            raise PermissionError("write path is outside Toolsmith allowed paths")
        return self.security_policy.sandbox.resolve_write(relative_path)

    def intent(
        self,
        *,
        permission: str,
        action_id: str,
        risk: ToolRisk,
        target: str,
        write_path: str | None = None,
        write_bytes: int = 0,
        network_host: str | None = None,
        executable: str | None = None,
        approval_required: bool = False,
    ) -> ActionIntent:
        binding = self._binding(permission)
        if _RISK_RANK[risk] > _RISK_RANK[binding.max_risk]:
            raise PermissionError("action risk exceeds the bound permission risk ceiling")
        if write_path is not None:
            self.resolve_write(write_path)
        return ActionIntent(
            action_id=action_id,
            tool_id=binding.tool_id,
            risk=risk,
            target=target,
            write_path=write_path,
            write_bytes=write_bytes,
            network_host=network_host,
            executable=executable,
            approval_required=(
                approval_required
                or risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}
            ),
        )

    def _binding(self, permission: str) -> CapabilityToolBinding:
        for binding in self.bindings:
            if binding.permission == permission:
                return binding
        raise PermissionError("permission has no downstream tool binding")


def build_toolsmith_security_envelope(
    job: CodingJob,
    *,
    bindings: tuple[CapabilityToolBinding, ...],
    budget: DownstreamBudgetLimits,
    require_untrusted_execution: bool = False,
) -> ToolsmithSecurityEnvelope:
    """Adapt a canonical Toolsmith job into downstream M10 guardrails.

    This bridge never upgrades Toolsmith's declared isolation. POLICY_ONLY and
    PROCESS_CONTAINED are not treated as a filesystem/network sandbox for untrusted
    candidate execution. Callers that intend to execute candidate code must request
    ``require_untrusted_execution`` and provide an OS/remote-sandboxed lease.
    """
    if not bindings:
        raise ValueError("at least one downstream capability binding is required")

    seen_permissions: set[str] = set()
    seen_tools: set[str] = set()
    for binding in bindings:
        if binding.permission in seen_permissions:
            raise ValueError(f"duplicate permission binding: {binding.permission}")
        if binding.tool_id in seen_tools:
            raise ValueError(f"duplicate tool binding: {binding.tool_id}")
        seen_permissions.add(binding.permission)
        seen_tools.add(binding.tool_id)
        if binding.permission not in job.permission_ceiling:
            raise PermissionError(
                f"binding permission is outside job permission ceiling: {binding.permission}"
            )

    untrusted_execution_ready = (
        _ISOLATION_RANK[job.lease.isolation_class]
        >= _ISOLATION_RANK[IsolationClass.OS_SANDBOXED]
    )
    if require_untrusted_execution and not untrusted_execution_ready:
        raise PermissionError(
            "untrusted candidate execution requires OS_SANDBOXED or REMOTE_SANDBOXED isolation"
        )

    if job.network_policy.mode is NetworkMode.DENY:
        allowed_network_hosts: tuple[str, ...] = ()
    else:
        allowed_network_hosts = job.network_policy.approved_hosts

    execution_budget = ExecutionBudget(
        max_write_bytes=budget.max_write_bytes,
        max_network_calls=budget.max_network_calls,
        max_process_launches=budget.max_process_launches,
    )
    policy = SecurityPolicy(
        granted_tools=frozenset(binding.tool_id for binding in bindings),
        sandbox=SandboxPolicy(
            workspace_root=job.lease.workspace_root,
            writable_roots=job.allowed_paths.roots,
            allowed_network_hosts=allowed_network_hosts,
            allowed_executables=job.process_policy.allowed_executables,
        ),
        budget=execution_budget,
    )
    return ToolsmithSecurityEnvelope(
        job_id=job.job_id,
        workspace_root=policy.sandbox.workspace_root,
        isolation_class=job.lease.isolation_class,
        allowed_paths=job.allowed_paths,
        bindings=bindings,
        security_policy=policy,
        untrusted_execution_ready=untrusted_execution_ready,
    )