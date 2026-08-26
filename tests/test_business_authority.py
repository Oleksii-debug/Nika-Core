from __future__ import annotations

from nika_core.business_authority import (
    BusinessAuthorizationIntent,
    BusinessAuthorizationUse,
    trusted_business_authorization,
)


class _BrokenAuthority:
    def authorize(
        self,
        *,
        intent: BusinessAuthorizationIntent,
        evidence_ref: str,
    ) -> bool:
        raise RuntimeError(f"authority unavailable: {intent.fingerprint}:{evidence_ref}")


def _intent(
    subject_id: str,
    *,
    scope: str,
    use: BusinessAuthorizationUse = BusinessAuthorizationUse.ONE_TIME,
) -> BusinessAuthorizationIntent:
    return BusinessAuthorizationIntent(
        objective_id="objective-authority-contract",
        purpose="proposal.approve",
        subject_id=subject_id,
        bindings=(("scope", scope),),
        use=use,
    )


def test_business_authorization_default_is_fail_closed() -> None:
    assert not trusted_business_authorization(
        None,
        intent=_intent("proposal-1", scope="scope-a"),
        evidence_ref="caller:self-minted",
    )


def test_test_authority_models_exact_idempotent_one_time_binding(business_authority) -> None:
    evidence_ref = "approval:test:one-time"
    first = _intent("proposal-1", scope="scope-a")
    other = _intent("proposal-1", scope="scope-b")
    business_authority.allow_once(evidence_ref)

    assert trusted_business_authorization(
        business_authority,
        intent=first,
        evidence_ref=evidence_ref,
    )
    assert trusted_business_authorization(
        business_authority,
        intent=first,
        evidence_ref=evidence_ref,
    )
    assert not trusted_business_authorization(
        business_authority,
        intent=other,
        evidence_ref=evidence_ref,
    )


def test_standing_policy_is_separate_reusable_authority_and_can_be_revoked(
    business_authority,
) -> None:
    evidence_ref = "standing-policy:test:1"
    business_authority.allow_standing(evidence_ref)
    first = _intent(
        "message-1",
        scope="counterparty-a",
        use=BusinessAuthorizationUse.STANDING_POLICY,
    )
    second = _intent(
        "message-2",
        scope="counterparty-b",
        use=BusinessAuthorizationUse.STANDING_POLICY,
    )

    assert trusted_business_authorization(
        business_authority,
        intent=first,
        evidence_ref=evidence_ref,
    )
    assert trusted_business_authorization(
        business_authority,
        intent=second,
        evidence_ref=evidence_ref,
    )

    business_authority.revoke(evidence_ref)
    assert not trusted_business_authorization(
        business_authority,
        intent=first,
        evidence_ref=evidence_ref,
    )


def test_authority_exception_is_fail_closed() -> None:
    assert not trusted_business_authorization(
        _BrokenAuthority(),
        intent=_intent("proposal-1", scope="scope-a"),
        evidence_ref="approval:test:broken",
    )


def test_business_authorization_fingerprint_is_order_independent_for_named_bindings() -> None:
    first = BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="delivery.authorize",
        subject_id="delivery-1",
        bindings=(("project_id", "project-1"), ("artifact_ref", "artifact-1")),
    )
    reordered = BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="delivery.authorize",
        subject_id="delivery-1",
        bindings=(("artifact_ref", "artifact-1"), ("project_id", "project-1")),
    )
    changed = BusinessAuthorizationIntent(
        objective_id="objective-1",
        purpose="delivery.authorize",
        subject_id="delivery-1",
        bindings=(("artifact_ref", "artifact-2"), ("project_id", "project-1")),
    )

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != changed.fingerprint
