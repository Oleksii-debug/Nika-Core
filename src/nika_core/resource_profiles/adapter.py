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

    if type(scope) is not str or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    if type(owner_id) is not str or not owner_id.strip():
        raise ValueError("owner_id must be a non-empty string")
    if (
        type(max_concurrent) is not int
        or not 1 <= max_concurrent <= (1 << 63) - 1
    ):
        raise ValueError("max_concurrent must be a positive SQLite-sized integer")

    selected_policy = ResourceProfilePolicy() if policy is None else policy
    spec = selected_policy.profile_spec(profile)
    return ResourceBudget(
        scope=scope,
        owner_id=owner_id,
        max_concurrent=max_concurrent,
        max_cpu_percent=spec.max_cpu_percent,
        max_memory_percent=spec.max_memory_percent,
    )
