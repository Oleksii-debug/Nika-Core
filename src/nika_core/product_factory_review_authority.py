from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_orchestration import TeamPlan

_TEAM_PLAN_REF_PREFIX = "team-plan-sha256:"


class ProductFactoryReviewAuthorityError(ValueError):
    """Raised when PF4 review authority identity is malformed."""


@dataclass(frozen=True, slots=True)
class ProductFactoryReviewSubject:
    """Immutable exact candidate/reviewer subject presented to trusted authority.

    This object is evidence identity only. It never proves that ``reviewer_id`` is a
    trusted actor by itself; a host-owned ``ProductFactoryReviewAuthorityPort`` must
    authenticate/authorize the reviewer for this exact subject.
    """

    project_id: str
    component_id: str
    work_id: str
    repository_id: str
    base_sha: str
    result_sha: str
    diff_digest: str
    attempt: int
    producer_actor_id: str
    reviewer_id: str
    accepted: bool

    def __post_init__(self) -> None:
        identities = (
            self.project_id,
            self.component_id,
            self.work_id,
            self.repository_id,
            self.producer_actor_id,
            self.reviewer_id,
        )
        if not all(isinstance(value, str) and value.strip() for value in identities):
            raise ProductFactoryReviewAuthorityError(
                "review subject identity must be non-empty text"
            )
        _validate_sha(self.base_sha, "base_sha")
        _validate_sha(self.result_sha, "result_sha")
        _validate_digest(self.diff_digest, "diff_digest")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ProductFactoryReviewAuthorityError(
                "review subject attempt must be an exact positive integer"
            )
        if type(self.accepted) is not bool:
            raise ProductFactoryReviewAuthorityError(
                "review subject accepted must be an exact boolean"
            )
        if self.producer_actor_id == self.reviewer_id:
            raise ProductFactoryReviewAuthorityError(
                "independent reviewer must differ from candidate producer"
            )

    @property
    def fingerprint(self) -> str:
        payload = (
            "product-factory-review-subject-v1",
            self.project_id,
            self.component_id,
            self.work_id,
            self.repository_id,
            self.base_sha,
            self.result_sha,
            self.diff_digest,
            self.attempt,
            self.producer_actor_id,
            self.reviewer_id,
            self.accepted,
        )
        canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProductFactoryReviewAuthorityPort(Protocol):
    """Host-owned verifier for one exact independent-review decision.

    Returning anything other than literal ``True``, or raising, is fail-closed. A
    caller-supplied reviewer id or evidence reference is never positive authority by
    itself.
    """

    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool: ...


def team_plan_semantic_fingerprint(team_plan: TeamPlan) -> str:
    """Return a stable digest over the exact security-relevant TeamPlan content.

    ``plan_id`` alone is not authority: a caller must not be able to reuse one durable
    identifier with altered reviewer roles, scopes, permissions or review flags. The
    fingerprint is intentionally separate from historical plan-id generation so existing
    deterministic TeamPlan identities do not need to be redefined.
    """

    if not isinstance(team_plan, TeamPlan):
        raise ProductFactoryReviewAuthorityError("team plan must be a TeamPlan")
    if not isinstance(team_plan.project_id, str) or not team_plan.project_id.strip():
        raise ProductFactoryReviewAuthorityError("team plan project identity must not be empty")
    if not isinstance(team_plan.plan_id, str) or not team_plan.plan_id.strip():
        raise ProductFactoryReviewAuthorityError("team plan identity must not be empty")
    if not isinstance(team_plan.roles, tuple) or not team_plan.roles:
        raise ProductFactoryReviewAuthorityError("team plan requires roles")

    roles: list[dict[str, object]] = []
    for role in sorted(team_plan.roles, key=lambda item: item.role_id):
        if not isinstance(role.role_id, str) or not role.role_id.strip():
            raise ProductFactoryReviewAuthorityError("team plan roles require identities")
        if type(role.independent_review) is not bool:
            raise ProductFactoryReviewAuthorityError(
                "team plan independent_review must be an exact boolean"
            )
        for label, values in (
            ("capabilities", role.capabilities),
            ("component_ids", role.component_ids),
            ("reasons", role.reasons),
            ("evidence_refs", role.evidence_refs),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ProductFactoryReviewAuthorityError(
                    f"team plan role {label} must contain non-empty text"
                )
        if not isinstance(role.permissions, frozenset) or any(
            not isinstance(value, str) or not value.strip() for value in role.permissions
        ):
            raise ProductFactoryReviewAuthorityError(
                "team plan role permissions must contain non-empty text"
            )
        roles.append(
            {
                "role_id": role.role_id,
                "capabilities": sorted(role.capabilities),
                "component_ids": sorted(role.component_ids),
                "permissions": sorted(role.permissions),
                "reasons": sorted(role.reasons),
                "evidence_refs": sorted(role.evidence_refs),
                "independent_review": role.independent_review,
            }
        )

    if not isinstance(team_plan.permission_ceiling, frozenset) or any(
        not isinstance(value, str) or not value.strip() for value in team_plan.permission_ceiling
    ):
        raise ProductFactoryReviewAuthorityError(
            "team plan permission ceiling must contain non-empty text"
        )
    if not isinstance(team_plan.reasons, tuple) or any(
        not isinstance(value, str) or not value.strip() for value in team_plan.reasons
    ):
        raise ProductFactoryReviewAuthorityError(
            "team plan reasons must contain non-empty text"
        )

    payload = {
        "schema": "product-factory-team-plan-v1",
        "project_id": team_plan.project_id,
        "plan_id": team_plan.plan_id,
        "roles": roles,
        "permission_ceiling": sorted(team_plan.permission_ceiling),
        "reasons": sorted(team_plan.reasons),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def team_plan_fingerprint_ref(team_plan: TeamPlan) -> str:
    """Return the durable ProductProject ``team_refs`` binding for exact plan content."""

    return f"{_TEAM_PLAN_REF_PREFIX}{team_plan_semantic_fingerprint(team_plan)}"


@dataclass(frozen=True, slots=True)
class TeamPlanReviewAuthority:
    """Scope trusted exact-review evidence to the canonical PF team assignment.

    ``TeamPlan`` is the assignment authority: only a role explicitly marked for
    independent review and assigned to the exact component may review it. The delegated
    ``evidence_authority`` remains the host-owned authenticity boundary proving that the
    exact review evidence was really issued; this adapter never treats caller text or a
    role id alone as proof.
    """

    team_plan: TeamPlan
    evidence_authority: ProductFactoryReviewAuthorityPort

    def __post_init__(self) -> None:
        # Reuse the exact-content validator used by the durable fingerprint boundary.
        team_plan_semantic_fingerprint(self.team_plan)
        role_ids = [role.role_id for role in self.team_plan.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ProductFactoryReviewAuthorityError("team plan role identities must be unique")

    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        if subject.project_id != self.team_plan.project_id or not evidence_refs:
            return False
        reviewer = next(
            (role for role in self.team_plan.roles if role.role_id == subject.reviewer_id),
            None,
        )
        if reviewer is None or not reviewer.independent_review:
            return False
        if subject.component_id not in reviewer.component_ids:
            return False
        return self.evidence_authority.verify(subject, evidence_refs) is True


def _validate_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 40-character hexadecimal SHA"
        )


def _validate_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 64-character hexadecimal digest"
        )