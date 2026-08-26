from __future__ import annotations

from nika_core.resource_profiles.contracts import ResourceProfileName
from nika_core.resource_profiles.policy import ResourceProfilePolicy
from nika_core.resources.contracts import ResourceBudget


def budget_for_profile(
    *,
    profile: ResourceProfileName | str,
    scope: str,
    owner_id: str,
    max_concurrent: int = 1,
    policy: ResourceProfilePolicy | None = None,
) -> ResourceBudget:
    """Project a named profile onto the existing lower-level ResourceBudget contract."""

    if not scope.strip():
        raise ValueError("scope must not be empty")
    if not owner_id.strip():
        raise ValueError("owner_id must not be empty")
    if max_concurrent <= 0:
        raise ValueError("max_concurrent must be greater than zero")

    selected_policy = ResourceProfilePolicy() if policy is None else policy
    spec = selected_policy.profile_spec(profile)
    return ResourceBudget(
        scope=scope,
        owner_id=owner_id,
        max_concurrent=max_concurrent,
        max_cpu_percent=spec.max_cpu_percent,
        max_memory_percent=spec.max_memory_percent,
    )
