from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


class ProductFactoryReviewAuthorityError(ValueError):
    """Raised when PF4 review authority identity is malformed."""


@dataclass(frozen=True, slots=True)
class ProductFactoryReviewSubject:
    """Immutable exact candidate/reviewer subject presented to trusted authority.

    This object is evidence identity only. It never proves that ``reviewer_id`` is a
    trusted actor by itself; a host-owned ``ProductFactoryReviewAuthorityPort`` must
    authenticate and authorize the reviewer for this exact subject.
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
        if any(not isinstance(value, str) or not value.strip() for value in identities):
            raise ProductFactoryReviewAuthorityError(
                "review subject identity must be non-empty text"
            )
        _validate_sha(self.base_sha, "base_sha")
        _validate_sha(self.result_sha, "result_sha")
        _validate_digest(self.diff_digest, "diff_digest")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ProductFactoryReviewAuthorityError("review subject attempt must be positive")
        if type(self.accepted) is not bool:
            raise ProductFactoryReviewAuthorityError("review decision must be an exact bool")
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

    Implementations may adapt a canonical repository/team review authority. Returning
    anything other than literal ``True``, or raising, is fail-closed.
    """

    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool: ...


def _validate_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 40-character hexadecimal SHA"
        )


def _validate_digest(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value.casefold()
    ):
        raise ProductFactoryReviewAuthorityError(
            f"{label} must be a 64-character hexadecimal digest"
        )
