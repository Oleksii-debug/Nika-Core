from __future__ import annotations

from nika_core.business_authority import (
    BusinessAuthorizationIntent,
    trusted_business_authorization,
)


class _Authority:
    def __init__(self) -> None:
        self.allowed: set[str] = set()
        self.used: dict[str, str] = {}

    def allow(self, evidence_ref: str) -> None:
        self.allowed.add(evidence_ref)

    def authorize(self, *, intent: BusinessAuthorizationIntent, evidence_ref: str) -> bool:
        if evidence_ref not in self.allowed:
            return False
        previous = self.used.get(evidence_ref)
        if previous is None:
            self.used[evidence_ref] = intent.fingerprint
            return True
        return previous == intent.fingerprint


def _intent(*, scope: str = "scope-a") -> BusinessAuthorizationIntent:
    return BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="work_order.authorize",
        subject_id="work-order-1",
        bindings=(("scope", scope), ("project_id", "project-1")),
    )


def test_intent_fingerprint_is_canonical_across_binding_order() -> None:
    first = BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="work_order.authorize",
        subject_id="work-order-1",
        bindings=(("scope", "scope-a"), ("project_id", "project-1")),
    )
    second = BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="work_order.authorize",
        subject_id="work-order-1",
        bindings=(("project_id", "project-1"), ("scope", "scope-a")),
    )
    assert first.fingerprint == second.fingerprint


def test_trusted_authorization_fails_closed_without_authority() -> None:
    assert (
        trusted_business_authorization(
            None,
            intent=_intent(),
            evidence_ref="approval:test:1",
        )
        is False
    )


def test_one_time_evidence_cannot_authorize_different_intent() -> None:
    authority = _Authority()
    authority.allow("approval:test:1")
    assert trusted_business_authorization(
        authority,
        intent=_intent(scope="scope-a"),
        evidence_ref="approval:test:1",
    )
    assert not trusted_business_authorization(
        authority,
        intent=_intent(scope="scope-b"),
        evidence_ref="approval:test:1",
    )


def test_authority_exception_fails_closed() -> None:
    class _BrokenAuthority:
        def authorize(self, *, intent, evidence_ref):
            raise RuntimeError("trusted authority unavailable")

    assert not trusted_business_authorization(
        _BrokenAuthority(),
        intent=_intent(),
        evidence_ref="approval:test:1",
    )
