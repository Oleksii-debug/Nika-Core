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

    Implementations may adapt the canonical M10/R4 review authority once integrated.
    Returning anything other than literal ``True``, or raising, is fail-closed.
    """

    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool: ...


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
