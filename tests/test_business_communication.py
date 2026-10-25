from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.business_communication import (
    BusinessCommunicationCoordinator,
    BusinessCommunicationError,
    BusinessCommunicationRepository,
    CommunicationState,
    StaleCommunicationStateError,
    communication_policy_ref,
    dump_business_communication,
    load_business_communication,
)
from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactorySnapshot,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import EvidenceRef, ResearchEvidencePackage


def _snapshot(
    authority: CommunicationAuthority = CommunicationAuthority.APPROVAL_REQUIRED,
    *,
    standing_policy_ref: str | None = None,
) -> BusinessFactorySnapshot:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-communication-1",
            goal="Use only a controlled sandbox communication channel",
            research_package=ResearchEvidencePackage(
                package_id="research-communication-1",
                evidence=(
                    EvidenceRef(
                        "evidence-channel",
                        "research:source:public:channel-policy",
                        "Sandbox channel is approved for the test",
                    ),
                ),
                research_artifact_ref="research:artifact:communication-1",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-communication-1",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=authority,
            standing_policy_ref=standing_policy_ref,
        ),
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-communication-1",
        title="Controlled sandbox outreach",
        evidence_ids=("evidence-channel",),
    )
    factory.create_lead(
        lead_id="lead-communication-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:communication-1",
    )
    return factory.snapshot()


def _draft(snapshot: BusinessFactorySnapshot):
    return BusinessCommunicationCoordinator.draft(
        snapshot,
        message_id="message-1",
        thread_ref="thread:sandbox:1",
        payload_ref="artifact:communication-draft:1",
    )


def test_approval_required_communication_records_durable_authority_and_result(
    business_authority,
) -> None:
    snapshot = _snapshot()
    draft = _draft(snapshot)
    assert draft.state is CommunicationState.DRAFT
    assert draft.row_version == 1
    assert draft.authorization_ref is None
    assert draft.authorization_fingerprint is None

    default_coordinator = BusinessCommunicationCoordinator()
    with pytest.raises(BusinessCommunicationError, match="approval_ref"):
        default_coordinator.authorize(draft, snapshot)

    with pytest.raises(BusinessCommunicationError, match="trusted communication approval"):
        default_coordinator.authorize(
            draft,
            snapshot,
            approval_ref="caller:self-minted-communication-approval",
        )

    business_authority.allow_once("approval:communication:1")
    trusted_coordinator = BusinessCommunicationCoordinator(
        approval_authority=business_authority
    )
    authorized = trusted_coordinator.authorize(
        draft,
        snapshot,
        approval_ref="approval:communication:1",
    )
    assert authorized.state is CommunicationState.AUTHORIZED
    assert authorized.authorization_ref == "approval:communication:1"
    assert authorized.authorization_fingerprint
    assert authorized.row_version == 2

    sent = BusinessCommunicationCoordinator.record_provider_result(
        authorized,
        snapshot,
        provider_evidence_ref="provider:sandbox:message:accepted:1",
    )
    assert sent.state is CommunicationState.SENT
    assert sent.row_version == 3
    assert sent.provider_evidence_ref == "provider:sandbox:message:accepted:1"
    assert load_business_communication(dump_business_communication(sent)) == sent

    with pytest.raises(BusinessCommunicationError, match="authorized communication"):
        BusinessCommunicationCoordinator.record_provider_result(
            sent,
            snapshot,
            provider_evidence_ref="provider:sandbox:duplicate:1",
        )


def test_draft_only_policy_never_creates_send_authority(business_authority) -> None:
    snapshot = _snapshot(CommunicationAuthority.DRAFT_ONLY)
    draft = _draft(snapshot)
    business_authority.allow_once("approval:must-not-override-policy")
    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    with pytest.raises(BusinessCommunicationError, match="draft-only policy"):
        coordinator.authorize(
            draft,
            snapshot,
            approval_ref="approval:must-not-override-policy",
        )
    assert draft.state is CommunicationState.DRAFT
    assert draft.authorization_ref is None


def test_standing_policy_authorization_is_trusted_scoped_and_revocable(
    business_authority,
) -> None:
    standing_ref = "standing-policy:communication:test:1"
    snapshot = _snapshot(
        CommunicationAuthority.STANDING_POLICY,
        standing_policy_ref=standing_ref,
    )
    draft = _draft(snapshot)
    with pytest.raises(BusinessCommunicationError, match="trusted communication approval"):
        BusinessCommunicationCoordinator().authorize(draft, snapshot)

    business_authority.allow_standing(standing_ref)
    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    authorized = coordinator.authorize(draft, snapshot)
    assert authorized.authorization_ref == standing_ref
    assert authorized.authorization_fingerprint
    assert communication_policy_ref(snapshot.policy) == standing_ref

    business_authority.revoke(standing_ref)
    other = replace(draft, message_id="message-revoked")
    with pytest.raises(BusinessCommunicationError, match="trusted communication approval"):
        coordinator.authorize(other, snapshot)


def test_policy_or_lead_change_requires_redraft_before_authorization(
    business_authority,
) -> None:
    snapshot = _snapshot()
    draft = _draft(snapshot)
    changed_policy = BusinessPolicy(
        policy_id="policy-communication-2",
        allowed_channel_ids=("sandbox-email",),
        communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
    )
    changed_snapshot = replace(snapshot, policy=changed_policy)
    business_authority.allow_once("approval:communication:2")
    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    with pytest.raises(BusinessCommunicationError, match="policy changed"):
        coordinator.authorize(
            draft,
            changed_snapshot,
            approval_ref="approval:communication:2",
        )


def test_provider_result_requires_authorization_and_exactly_one_outcome(
    business_authority,
) -> None:
    snapshot = _snapshot()
    draft = _draft(snapshot)
    with pytest.raises(BusinessCommunicationError, match="authorized communication"):
        BusinessCommunicationCoordinator.record_provider_result(
            draft,
            snapshot,
            failure_ref="provider:sandbox:failure:before-authorization",
        )

    business_authority.allow_once("approval:communication:1")
    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    authorized = coordinator.authorize(
        draft,
        snapshot,
        approval_ref="approval:communication:1",
    )
    with pytest.raises(BusinessCommunicationError, match="exactly one"):
        BusinessCommunicationCoordinator.record_provider_result(authorized, snapshot)
    with pytest.raises(BusinessCommunicationError, match="exactly one"):
        BusinessCommunicationCoordinator.record_provider_result(
            authorized,
            snapshot,
            provider_evidence_ref="provider:sandbox:success:1",
            failure_ref="provider:sandbox:failure:1",
        )

    failed = BusinessCommunicationCoordinator.record_provider_result(
        authorized,
        snapshot,
        failure_ref="provider:sandbox:failure:1",
    )
    assert failed.state is CommunicationState.FAILED
    assert failed.failure_ref == "provider:sandbox:failure:1"


def test_communication_repository_survives_restart_and_rejects_stale_writer(
    tmp_path,
    business_authority,
) -> None:
    database_path = tmp_path / "nika.sqlite"
    store = SQLiteStore(database_path)
    store.initialize()
    repository = BusinessCommunicationRepository(store)
    repository.initialize()

    snapshot = _snapshot()
    draft = _draft(snapshot)
    repository.save(draft, expected_row_version=0)
    loaded = repository.load("message-1")
    assert loaded == draft

    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    business_authority.allow_once("approval:communication:1")
    authorized = coordinator.authorize(
        loaded,
        snapshot,
        approval_ref="approval:communication:1",
    )
    repository.save(authorized, expected_row_version=draft.row_version)

    business_authority.allow_once("approval:communication:stale")
    stale_authorized = coordinator.authorize(
        draft,
        snapshot,
        approval_ref="approval:communication:stale",
    )
    with pytest.raises(StaleCommunicationStateError, match="row version changed"):
        repository.save(stale_authorized, expected_row_version=draft.row_version)

    restarted_store = SQLiteStore(database_path)
    restarted_store.initialize()
    restarted_repository = BusinessCommunicationRepository(restarted_store)
    restarted_repository.initialize()
    restored = restarted_repository.load("message-1")
    assert restored == authorized

    failed = BusinessCommunicationCoordinator.record_provider_result(
        restored,
        snapshot,
        failure_ref="provider:sandbox:timeout:1",
    )
    restarted_repository.save(
        failed,
        expected_row_version=restored.row_version,
    )
    assert restarted_repository.load("message-1") == failed


def test_authorization_fingerprint_rejects_message_scope_tamper(business_authority) -> None:
    snapshot = _snapshot()
    draft = _draft(snapshot)
    business_authority.allow_once("approval:communication:scope")
    coordinator = BusinessCommunicationCoordinator(approval_authority=business_authority)
    authorized = coordinator.authorize(
        draft,
        snapshot,
        approval_ref="approval:communication:scope",
    )
    forged = replace(authorized, payload_ref="artifact:attacker-replaced-payload")
    with pytest.raises(BusinessCommunicationError, match="fingerprint does not match"):
        dump_business_communication(forged)


def test_corrupt_persisted_communication_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    repository = BusinessCommunicationRepository(store)
    repository.initialize()
    draft = _draft(_snapshot())
    repository.save(draft, expected_row_version=0)

    with store.connection() as conn:
        conn.execute(
            "UPDATE business_communications SET payload_json = ? WHERE message_id = ?",
            ("{}", draft.message_id),
        )

    with pytest.raises(BusinessCommunicationError, match="fields do not match schema"):
        repository.load(draft.message_id)


def test_pf9_communication_contract_has_no_external_send_executor() -> None:
    assert not hasattr(BusinessCommunicationCoordinator, "send")
    assert not hasattr(BusinessCommunicationCoordinator, "publish")
    assert not hasattr(BusinessCommunicationCoordinator, "pay")
