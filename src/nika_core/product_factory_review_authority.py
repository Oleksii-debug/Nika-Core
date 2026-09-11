from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_orchestration import TeamPlan


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
        if not all(value.strip() for value in identities):
            raise ProductFactoryReviewAuthorityError(
                "review subject identity must not be empty"
            )
        _validate_sha(self.base_sha, "base_sha")
        _validate_sha(self.result_sha, "result_sha")
        _validate_digest(self.diff_digest, "diff_digest")
        if self.attempt < 1:
            raise ProductFactoryReviewAuthorityError("review subject attempt must be positive")
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
        if not self.team_plan.project_id.strip() or not self.team_plan.plan_id.strip():
            raise ProductFactoryReviewAuthorityError("team plan identity must not be empty")
        role_ids = [role.role_id for role in self.team_plan.roles]
        if not role_ids or any(not role_id.strip() for role_id in role_ids):
            raise ProductFactoryReviewAuthorityError("team plan roles require identities")
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
    if len(value) != 40 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 40-character hexadecimal SHA"
        )


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 64-character hexadecimal digest"
        )
