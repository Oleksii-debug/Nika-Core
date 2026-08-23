from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from nika_core.product_factory_orchestration import (
    DynamicTeamComposer,
    TeamCompositionError,
    TeamCompositionRequest,
    TeamPlan,
    TeamRole,
)

TEAM_LIFECYCLE_SCHEMA_VERSION = 1


class TeamLifecycleError(TeamCompositionError):
    """Raised when deterministic PF2 team lifecycle invariants are violated."""


class RoleAssignmentStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    FAILED = "failed"
    REPLACED = "replaced"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class TeamRoleAssignment:
    assignment_id: str
    role: TeamRole
    status: RoleAssignmentStatus
    generation: int = 0
    replaces_assignment_id: str | None = None
    transition_reason: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.assignment_id.strip():
            raise TeamLifecycleError("assignment_id must not be empty")
        if self.generation < 0:
            raise TeamLifecycleError("assignment generation must not be negative")
        if self.transition_reason is not None and not self.transition_reason.strip():
            raise TeamLifecycleError("transition reason must not be empty")
        if any(not ref.strip() for ref in self.evidence_refs):
            raise TeamLifecycleError("assignment evidence references must not be empty")


@dataclass(frozen=True, slots=True)
class TeamLifecycleSnapshot:
    project_id: str
    revision: int
    permission_ceiling: frozenset[str]
    assignments: tuple[TeamRoleAssignment, ...]
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_snapshot(self)

    @property
    def current_assignments(self) -> tuple[TeamRoleAssignment, ...]:
        terminal = {RoleAssignmentStatus.REPLACED, RoleAssignmentStatus.RETIRED}
        latest: dict[str, TeamRoleAssignment] = {}
        for assignment in self.assignments:
            if assignment.status in terminal:
                continue
            previous = latest.get(assignment.role.role_id)
            if previous is not None:
                raise TeamLifecycleError(
                    f"multiple current assignments for role {assignment.role.role_id}"
                )
            latest[assignment.role.role_id] = assignment
        return tuple(sorted(latest.values(), key=lambda item: item.role.role_id))

    def to_team_plan(self) -> TeamPlan:
        roles = tuple(assignment.role for assignment in self.current_assignments)
        return TeamPlan(
            project_id=self.project_id,
            plan_id=_snapshot_plan_id(self.project_id, self.permission_ceiling, roles),
            roles=roles,
            permission_ceiling=self.permission_ceiling,
            reasons=self.reasons,
        )

    def to_json(self) -> str:
        payload = {
            "schema_version": TEAM_LIFECYCLE_SCHEMA_VERSION,
            "project_id": self.project_id,
            "revision": self.revision,
            "permission_ceiling": sorted(self.permission_ceiling),
            "reasons": list(self.reasons),
            "assignments": [_assignment_to_payload(item) for item in self.assignments],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> TeamLifecycleSnapshot:
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TeamLifecycleError("team lifecycle payload is not valid JSON") from exc
        data = _require_object(
            raw,
            {
                "schema_version",
                "project_id",
                "revision",
                "permission_ceiling",
                "reasons",
                "assignments",
            },
            "team lifecycle payload",
        )
        schema_version = _require_int(data["schema_version"], "schema_version")
        if schema_version != TEAM_LIFECYCLE_SCHEMA_VERSION:
            raise TeamLifecycleError(
                f"unsupported team lifecycle schema_version {schema_version}"
            )
        project_id = _require_text(data["project_id"], "project_id")
        revision = _require_int(data["revision"], "revision")
        if revision < 0:
            raise TeamLifecycleError("revision must not be negative")
        permission_ceiling = frozenset(
            _require_unique_text_list(data["permission_ceiling"], "permission_ceiling")
        )
        reasons = tuple(_require_text_list(data["reasons"], "reasons"))
        assignments_raw = _require_list(data["assignments"], "assignments")
        assignments = tuple(
            _assignment_from_payload(item, index) for index, item in enumerate(assignments_raw)
        )
        return cls(
            project_id=project_id,
            revision=revision,
            permission_ceiling=permission_ceiling,
            assignments=assignments,
            reasons=reasons,
        )


class DynamicTeamLifecycle:
    """Collision-free PF2 lifecycle adapter over the integrated deterministic composer."""

    def __init__(self, composer: DynamicTeamComposer | None = None) -> None:
        self._composer = composer or DynamicTeamComposer()

    def compose(self, request: TeamCompositionRequest) -> TeamLifecycleSnapshot:
        plan = self._composer.compose(request)
        assignments = tuple(
            _new_assignment(request.project_id, role, generation=0) for role in plan.roles
        )
        return TeamLifecycleSnapshot(
            project_id=request.project_id,
            revision=0,
            permission_ceiling=request.permission_ceiling,
            assignments=assignments,
            reasons=plan.reasons,
        )

    def add_specialist(
        self,
        snapshot: TeamLifecycleSnapshot,
        *,
        specialization: str,
        component_ids: tuple[str, ...],
        requested_permissions: frozenset[str],
        reason: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> TeamLifecycleSnapshot:
        capability = specialization.strip().casefold()
        if not capability:
            raise TeamLifecycleError("specialization must not be empty")
        if not reason.strip():
            raise TeamLifecycleError("specialist reason must not be empty")
        components = tuple(sorted(set(component_ids)))
        if not components:
            raise TeamLifecycleError("specialist must own at least one component")
        if any(not component_id.strip() for component_id in components):
            raise TeamLifecycleError("specialist component identity must not be empty")
        if any(not ref.strip() for ref in evidence_refs):
            raise TeamLifecycleError("specialist evidence references must not be empty")

        known_components = {
            component_id
            for assignment in snapshot.current_assignments
            for component_id in assignment.role.component_ids
        }
        unknown = set(components) - known_components
        if unknown:
            raise TeamLifecycleError(
                "specialist references unknown component(s): " + ", ".join(sorted(unknown))
            )

        for assignment in snapshot.current_assignments:
            role = assignment.role
            if (
                role.capabilities == (capability,)
                and role.component_ids == components
                and not role.independent_review
            ):
                return snapshot

        permissions = frozenset(requested_permissions) & snapshot.permission_ceiling
        role = TeamRole(
            role_id=_stable_id(
                "team-specialist",
                snapshot.project_id,
                capability,
                components,
            ),
            capabilities=(capability,),
            component_ids=components,
            permissions=permissions,
            reasons=(reason,),
            evidence_refs=evidence_refs,
        )
        if any(item.role.role_id == role.role_id for item in snapshot.assignments):
            raise TeamLifecycleError("specialist role identity collision")

        assignment = _new_assignment(snapshot.project_id, role, generation=0)
        return TeamLifecycleSnapshot(
            project_id=snapshot.project_id,
            revision=snapshot.revision + 1,
            permission_ceiling=snapshot.permission_ceiling,
            assignments=(*snapshot.assignments, assignment),
            reasons=(*snapshot.reasons, f"added specialist {capability}: {reason}"),
        )

    def mark_unavailable(
        self,
        snapshot: TeamLifecycleSnapshot,
        *,
        role_id: str,
        status: RoleAssignmentStatus,
        reason: str,
        evidence_refs: tuple[str, ...],
    ) -> TeamLifecycleSnapshot:
        if status not in {RoleAssignmentStatus.BLOCKED, RoleAssignmentStatus.FAILED}:
            raise TeamLifecycleError("only blocked or failed status can mark a role unavailable")
        if not reason.strip():
            raise TeamLifecycleError("unavailable role reason must not be empty")
        if not evidence_refs or any(not ref.strip() for ref in evidence_refs):
            raise TeamLifecycleError("unavailable role transition requires evidence")

        current = _current_assignment(snapshot, role_id)
        updated = replace(
            current,
            status=status,
            transition_reason=reason,
            evidence_refs=evidence_refs,
        )
        assignments = tuple(
            updated if item.assignment_id == current.assignment_id else item
            for item in snapshot.assignments
        )
        return TeamLifecycleSnapshot(
            project_id=snapshot.project_id,
            revision=snapshot.revision + 1,
            permission_ceiling=snapshot.permission_ceiling,
            assignments=assignments,
            reasons=(*snapshot.reasons, f"{role_id} marked {status.value}: {reason}"),
        )

    def replace_unavailable(
        self,
        snapshot: TeamLifecycleSnapshot,
        *,
        role_id: str,
        reason: str,
        evidence_refs: tuple[str, ...],
    ) -> TeamLifecycleSnapshot:
        if not reason.strip():
            raise TeamLifecycleError("replacement reason must not be empty")
        if not evidence_refs or any(not ref.strip() for ref in evidence_refs):
            raise TeamLifecycleError("replacement requires evidence")

        current = _current_assignment(snapshot, role_id)
        if current.status not in {RoleAssignmentStatus.BLOCKED, RoleAssignmentStatus.FAILED}:
            raise TeamLifecycleError("only blocked or failed assignments can be replaced")

        retired = replace(
            current,
            status=RoleAssignmentStatus.REPLACED,
            transition_reason=reason,
            evidence_refs=evidence_refs,
        )
        generation = current.generation + 1
        replacement = TeamRoleAssignment(
            assignment_id=_assignment_id(snapshot.project_id, current.role.role_id, generation),
            role=current.role,
            status=RoleAssignmentStatus.ACTIVE,
            generation=generation,
            replaces_assignment_id=current.assignment_id,
            transition_reason=reason,
            evidence_refs=evidence_refs,
        )
        assignments = tuple(
            retired if item.assignment_id == current.assignment_id else item
            for item in snapshot.assignments
        )
        assignments = (*assignments, replacement)
        return TeamLifecycleSnapshot(
            project_id=snapshot.project_id,
            revision=snapshot.revision + 1,
            permission_ceiling=snapshot.permission_ceiling,
            assignments=assignments,
            reasons=(*snapshot.reasons, f"replaced unavailable assignment for {role_id}: {reason}"),
        )

    def recompose(
        self,
        snapshot: TeamLifecycleSnapshot,
        request: TeamCompositionRequest,
    ) -> TeamLifecycleSnapshot:
        if request.project_id != snapshot.project_id:
            raise TeamLifecycleError("recomposition project_id does not match lifecycle snapshot")
        if not request.permission_ceiling <= snapshot.permission_ceiling:
            raise TeamLifecycleError(
                "recomposition cannot widen the existing project permission ceiling"
            )

        desired_plan = self._composer.compose(request)
        current_by_key = {
            _role_semantic_key(assignment.role): assignment
            for assignment in snapshot.current_assignments
        }
        desired_keys: set[tuple[tuple[str, ...], tuple[str, ...], bool]] = set()
        updated_assignments = list(snapshot.assignments)
        changed = request.permission_ceiling != snapshot.permission_ceiling

        for desired_role in desired_plan.roles:
            key = _role_semantic_key(desired_role)
            desired_keys.add(key)
            existing = current_by_key.get(key)
            if existing is None:
                historical = _latest_historical_by_key(snapshot, key)
                if historical is None:
                    role = desired_role
                    generation = 0
                    replaces_assignment_id = None
                else:
                    role = replace(
                        desired_role,
                        role_id=historical.role.role_id,
                    )
                    generation = historical.generation + 1
                    replaces_assignment_id = historical.assignment_id
                role = replace(
                    role,
                    permissions=role.permissions & request.permission_ceiling,
                )
                updated_assignments.append(
                    TeamRoleAssignment(
                        assignment_id=_assignment_id(
                            snapshot.project_id,
                            role.role_id,
                            generation,
                        ),
                        role=role,
                        status=RoleAssignmentStatus.ACTIVE,
                        generation=generation,
                        replaces_assignment_id=replaces_assignment_id,
                        transition_reason="role activated by deterministic recomposition",
                    )
                )
                changed = True
                continue

            attenuated = existing.role.permissions & request.permission_ceiling
            if attenuated != existing.role.permissions:
                replacement_assignment = replace(
                    existing,
                    role=replace(existing.role, permissions=attenuated),
                )
                updated_assignments = [
                    replacement_assignment
                    if item.assignment_id == existing.assignment_id
                    else item
                    for item in updated_assignments
                ]
                changed = True

        for existing in snapshot.current_assignments:
            if _role_semantic_key(existing.role) in desired_keys:
                continue
            retired = replace(
                existing,
                status=RoleAssignmentStatus.RETIRED,
                transition_reason="role retired by deterministic recomposition",
            )
            updated_assignments = [
                retired if item.assignment_id == existing.assignment_id else item
                for item in updated_assignments
            ]
            changed = True

        reasons = tuple(dict.fromkeys((*snapshot.reasons, *desired_plan.reasons)))
        if reasons != snapshot.reasons:
            changed = True
        if not changed:
            return snapshot
        return TeamLifecycleSnapshot(
            project_id=snapshot.project_id,
            revision=snapshot.revision + 1,
            permission_ceiling=request.permission_ceiling,
            assignments=tuple(updated_assignments),
            reasons=reasons,
        )


def _new_assignment(
    project_id: str,
    role: TeamRole,
    *,
    generation: int,
) -> TeamRoleAssignment:
    return TeamRoleAssignment(
        assignment_id=_assignment_id(project_id, role.role_id, generation),
        role=role,
        status=RoleAssignmentStatus.ACTIVE,
        generation=generation,
    )


def _assignment_id(project_id: str, role_id: str, generation: int) -> str:
    return _stable_id("team-assignment", project_id, role_id, generation)


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _snapshot_plan_id(
    project_id: str,
    permission_ceiling: frozenset[str],
    roles: tuple[TeamRole, ...],
) -> str:
    payload = tuple(
        (
            role.role_id,
            role.capabilities,
            role.component_ids,
            tuple(sorted(role.permissions)),
            role.independent_review,
        )
        for role in roles
    )
    return _stable_id("team-plan-lifecycle", project_id, tuple(sorted(permission_ceiling)), payload)


def _role_semantic_key(role: TeamRole) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    return (role.capabilities, role.component_ids, role.independent_review)


def _current_assignment(snapshot: TeamLifecycleSnapshot, role_id: str) -> TeamRoleAssignment:
    matches = [
        assignment
        for assignment in snapshot.current_assignments
        if assignment.role.role_id == role_id
    ]
    if not matches:
        raise TeamLifecycleError(f"unknown current role_id {role_id}")
    return matches[0]


def _latest_historical_by_key(
    snapshot: TeamLifecycleSnapshot,
    key: tuple[tuple[str, ...], tuple[str, ...], bool],
) -> TeamRoleAssignment | None:
    historical = [
        assignment
        for assignment in snapshot.assignments
        if _role_semantic_key(assignment.role) == key
    ]
    if not historical:
        return None
    return max(historical, key=lambda item: item.generation)


def _validate_snapshot(snapshot: TeamLifecycleSnapshot) -> None:
    if not snapshot.project_id.strip():
        raise TeamLifecycleError("project_id must not be empty")
    if snapshot.revision < 0:
        raise TeamLifecycleError("revision must not be negative")
    if any(not permission.strip() for permission in snapshot.permission_ceiling):
        raise TeamLifecycleError("permission ceiling entries must not be empty")
    if any(not reason.strip() for reason in snapshot.reasons):
        raise TeamLifecycleError("snapshot reasons must not be empty")

    assignment_ids: set[str] = set()
    generations: dict[str, set[int]] = {}
    by_assignment_id: dict[str, TeamRoleAssignment] = {}
    current_count: dict[str, int] = {}
    terminal = {RoleAssignmentStatus.REPLACED, RoleAssignmentStatus.RETIRED}

    for assignment in snapshot.assignments:
        if assignment.assignment_id in assignment_ids:
            raise TeamLifecycleError("assignment ids must be unique")
        assignment_ids.add(assignment.assignment_id)
        by_assignment_id[assignment.assignment_id] = assignment
        role = assignment.role
        if not role.role_id.strip():
            raise TeamLifecycleError("role_id must not be empty")
        if not role.permissions <= snapshot.permission_ceiling:
            raise TeamLifecycleError("role permissions exceed project permission ceiling")
        seen_generations = generations.setdefault(role.role_id, set())
        if assignment.generation in seen_generations:
            raise TeamLifecycleError("role assignment generations must be unique")
        seen_generations.add(assignment.generation)
        if assignment.status not in terminal:
            current_count[role.role_id] = current_count.get(role.role_id, 0) + 1

    if any(count > 1 for count in current_count.values()):
        raise TeamLifecycleError("logical roles may have only one current assignment")

    current_semantics: set[tuple[tuple[str, ...], tuple[str, ...], bool]] = set()
    for assignment in snapshot.assignments:
        if assignment.status in terminal:
            continue
        semantic_key = _role_semantic_key(assignment.role)
        if semantic_key in current_semantics:
            raise TeamLifecycleError("current logical roles must be semantically deduplicated")
        current_semantics.add(semantic_key)

    for assignment in snapshot.assignments:
        replaced_id = assignment.replaces_assignment_id
        if replaced_id is None:
            if assignment.generation != 0:
                raise TeamLifecycleError(
                    "nonzero assignment generation requires predecessor identity"
                )
            continue
        predecessor = by_assignment_id.get(replaced_id)
        if predecessor is None:
            raise TeamLifecycleError("replacement predecessor assignment is missing")
        if predecessor.role.role_id != assignment.role.role_id:
            raise TeamLifecycleError("replacement predecessor belongs to a different logical role")
        if predecessor.generation + 1 != assignment.generation:
            raise TeamLifecycleError("replacement generation must follow predecessor exactly")


def _assignment_to_payload(assignment: TeamRoleAssignment) -> dict[str, Any]:
    role = assignment.role
    return {
        "assignment_id": assignment.assignment_id,
        "status": assignment.status.value,
        "generation": assignment.generation,
        "replaces_assignment_id": assignment.replaces_assignment_id,
        "transition_reason": assignment.transition_reason,
        "evidence_refs": list(assignment.evidence_refs),
        "role": {
            "role_id": role.role_id,
            "capabilities": list(role.capabilities),
            "component_ids": list(role.component_ids),
            "permissions": sorted(role.permissions),
            "reasons": list(role.reasons),
            "evidence_refs": list(role.evidence_refs),
            "independent_review": role.independent_review,
        },
    }


def _assignment_from_payload(value: object, index: int) -> TeamRoleAssignment:
    data = _require_object(
        value,
        {
            "assignment_id",
            "status",
            "generation",
            "replaces_assignment_id",
            "transition_reason",
            "evidence_refs",
            "role",
        },
        f"assignments[{index}]",
    )
    role_data = _require_object(
        data["role"],
        {
            "role_id",
            "capabilities",
            "component_ids",
            "permissions",
            "reasons",
            "evidence_refs",
            "independent_review",
        },
        f"assignments[{index}].role",
    )
    try:
        status = RoleAssignmentStatus(
            _require_text(data["status"], f"assignments[{index}].status")
        )
    except ValueError as exc:
        raise TeamLifecycleError(f"assignments[{index}].status is unsupported") from exc
    generation = _require_int(data["generation"], f"assignments[{index}].generation")
    if generation < 0:
        raise TeamLifecycleError(f"assignments[{index}].generation must not be negative")
    role = TeamRole(
        role_id=_require_text(role_data["role_id"], f"assignments[{index}].role.role_id"),
        capabilities=tuple(
            _require_text_list(
                role_data["capabilities"],
                f"assignments[{index}].role.capabilities",
            )
        ),
        component_ids=tuple(
            _require_text_list(
                role_data["component_ids"],
                f"assignments[{index}].role.component_ids",
            )
        ),
        permissions=frozenset(
            _require_unique_text_list(
                role_data["permissions"],
                f"assignments[{index}].role.permissions",
            )
        ),
        reasons=tuple(
            _require_text_list(role_data["reasons"], f"assignments[{index}].role.reasons")
        ),
        evidence_refs=tuple(
            _require_text_list(
                role_data["evidence_refs"],
                f"assignments[{index}].role.evidence_refs",
            )
        ),
        independent_review=_require_bool(
            role_data["independent_review"],
            f"assignments[{index}].role.independent_review",
        ),
    )
    replaces_assignment_id = _require_optional_text(
        data["replaces_assignment_id"],
        f"assignments[{index}].replaces_assignment_id",
    )
    transition_reason = _require_optional_text(
        data["transition_reason"],
        f"assignments[{index}].transition_reason",
    )
    return TeamRoleAssignment(
        assignment_id=_require_text(
            data["assignment_id"],
            f"assignments[{index}].assignment_id",
        ),
        role=role,
        status=status,
        generation=generation,
        replaces_assignment_id=replaces_assignment_id,
        transition_reason=transition_reason,
        evidence_refs=tuple(
            _require_text_list(
                data["evidence_refs"],
                f"assignments[{index}].evidence_refs",
            )
        ),
    )


def _require_object(value: object, expected_keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TeamLifecycleError(f"{field} must be an object")
    keys = set(value)
    if keys != expected_keys:
        missing = sorted(expected_keys - keys)
        extra = sorted(keys - expected_keys)
        raise TeamLifecycleError(f"{field} keys mismatch; missing={missing}, extra={extra}")
    return value


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TeamLifecycleError(f"{field} must be an array")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamLifecycleError(f"{field} must be a non-empty string")
    return value


def _require_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TeamLifecycleError(f"{field} must be an integer")
    return value


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TeamLifecycleError(f"{field} must be a boolean")
    return value


def _require_text_list(value: object, field: str) -> list[str]:
    items = _require_list(value, field)
    return [_require_text(item, f"{field}[{index}]") for index, item in enumerate(items)]


def _require_unique_text_list(value: object, field: str) -> list[str]:
    items = _require_text_list(value, field)
    if len(items) != len(set(items)):
        raise TeamLifecycleError(f"{field} entries must be unique")
    return items
