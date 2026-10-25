from __future__ import annotations

import pytest

from nika_core.business_authority import (
    BusinessAuthorizationIntent,
    BusinessAuthorizationUse,
)


class DeterministicBusinessAuthority:
    """Test-only authority that models exact binding, replay and revocation semantics."""

    def __init__(self) -> None:
        self._one_time_allowed: set[str] = set()
        self._standing_allowed: set[str] = set()
        self._bound: dict[str, str] = {}
        self._revoked: set[str] = set()

    def allow_once(self, evidence_ref: str) -> None:
        self._one_time_allowed.add(evidence_ref)

    def allow_standing(self, evidence_ref: str) -> None:
        self._standing_allowed.add(evidence_ref)

    def revoke(self, evidence_ref: str) -> None:
        self._revoked.add(evidence_ref)

    def authorize(
        self,
        *,
        intent: BusinessAuthorizationIntent,
        evidence_ref: str,
    ) -> bool:
        if evidence_ref in self._revoked:
            return False
        if intent.use is BusinessAuthorizationUse.STANDING_POLICY:
            return evidence_ref in self._standing_allowed
        if evidence_ref not in self._one_time_allowed:
            return False
        previous = self._bound.get(evidence_ref)
        if previous is None:
            self._bound[evidence_ref] = intent.fingerprint
            return True
        return previous == intent.fingerprint


@pytest.fixture
def business_authority() -> DeterministicBusinessAuthority:
    return DeterministicBusinessAuthority()
