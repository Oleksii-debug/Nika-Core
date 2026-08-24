from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactoryError,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    InvoicePaymentState,
    QAState,
    StaleBusinessStateError,
    dump_business_snapshot,
    load_business_snapshot,
)
from nika_core.business_factory_persistence import BusinessFactoryRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_compliance import ProductComplianceDecision
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceSnapshot,
    build_verified_notice_bundle,
)


def _research_package() -> ResearchEvidencePackage:
    return ResearchEvidencePackage(
        package_id="research-market-1",
        evidence=(
            EvidenceRef("evidence-demand", "research:source:public:1", "Demand exists"),
            EvidenceRef("evidence-price", "research:source:public:2", "Price range"),
        ),
        research_artifact_ref="research:result-set:1",
    )


def _factory(approval_authority=None) -> BusinessFactory:
    return BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-1",
            goal="Validate a controlled expense-app business offer",
            research_package=_research_package(),
        ),
        policy=BusinessPolicy(
            policy_id="policy-1",
            allowed_channel_ids=("sandbox-email", "test-marketplace"),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=approval_authority,
    )


def _advance_to_work_order(factory: BusinessFactory, approval_authority) -> None:
    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Small-team expense approval workflow",
        evidence_ids=("evidence-demand", "evidence-price"),
    )
    factory.create_lead(
        lead_id="lead-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:1",
    )
    factory.qualify_lead(qualification_ref="qualification:review:1")
    factory.draft_proposal(
        proposal_id="proposal-1",
        scope_summary="Build and deliver the approved test product scope.",
    )
    approval_authority.allow_once("approval:proposal:1")
    factory.approve_proposal(approval_ref="approval:proposal:1")
    approval_authority.allow_once("approval:work-order:1")
    factory.create_work_order(
        work_order_id="work-order-1",
        scope="Implement the approved expense-app test scope.",
        authorization_ref="approval:work-order:1",
    )


class _ReleaseReviewAuthority:
    def __init__(self, project_id: str) -> None:
        self._project_id = project_id

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (
            project_id == self._project_id
            and evidence_ref == f"review:release-scope:{project_id}"
            and purpose == "compliance-scope"
        )


def _allowed_release_grant(
    project_id: str,
    *,
    artifact_ref: str,
    bundle_dir,
):
    notice_bundle_sha256 = build_verified_notice_bundle(bundle_dir)
    snapshot = ReleaseComplianceSnapshot(
        project_id=project_id,
        release_id="release:test:1",
        project_source_ref="git:test:project-source",
        project_source_sha256="1" * 64,
        artifact_ref=artifact_ref,
        artifact_sha256="2" * 64,
        notice_bundle_sha256=notice_bundle_sha256,
        dependencies=(),
        scope_review_ref=f"review:release-scope:{project_id}",
    )
    gate = ProductReleaseComplianceGate(
        review_authority=_ReleaseReviewAuthority(project_id),
    )
    decision = gate.evaluate(snapshot, bundle_dir=bundle_dir)
    assert decision.allowed is True
    grant = gate.require_release_allowed(decision, snapshot, bundle_dir=bundle_dir)
    assert grant.allowed is True
    return grant


def test_business_flow_links_real_product_project_and_survives_restart(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    product_projects = ProductProjectRepository(store)
    business_store = BusinessFactoryRepository(store)
    business_store.initialize()

    factory = _factory(business_authority)
    first = factory.snapshot()
    assert first.row_version == 1
    business_store.save(first, expected_row_version=0)

    _advance_to_work_order(factory, business_authority)
    order = factory.handoff_to_product_factory(
        repository=product_projects,
        project_id="product-project-1",
        project_name="Expense App Test Product",
        spec=ProductProjectSpec(
            goal="Build the authorized expense app test product",
            desired_outcome="A QA-verified delivery candidate",
            compliance={"business_work_order_ref": "work-order-1"},
        ),
        idempotency_key="business-work-order-1-product-project",
    )
    assert order.product_project_id == "product-project-1"
    stored_product = product_projects.get("product-project-1")
    assert stored_product.project_id == "product-project-1"
    assert stored_product.spec.compliance["business_work_order_authorization_fingerprint"] == (
        order.authorization_fingerprint
    )

    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:report:1")
    artifact_ref = "artifact:expense-app:test:1"
    grant = _allowed_release_grant(
        "product-project-1",
        artifact_ref=artifact_ref,
        bundle_dir=tmp_path / "release-bundle",
    )
    business_authority.allow_once("approval:delivery:1")
    factory.record_delivery(
        delivery_id="delivery-1",
        artifact_ref=artifact_ref,
        authorization_ref="approval:delivery:1",
        compliance=grant,
    )
    factory.record_payment_state(
        invoice_ref="invoice:test:1",
        state=InvoicePaymentState.PAID,
        evidence_ref="payment-provider:status:paid:1",
    )
    factory.open_support_case(
        case_id="support-1",
        summary="Customer reported a test-channel export question.",
        evidence_ref="support:test:thread:1",
    )

    final_snapshot = factory.snapshot()
    business_store.save(final_snapshot, expected_row_version=first.row_version)

    restarted_store = SQLiteStore(tmp_path / "nika.sqlite")
    restarted_store.initialize()
    restarted_business_store = BusinessFactoryRepository(restarted_store)
    restarted_business_store.initialize()
    restored_snapshot = restarted_business_store.load("objective-1")
    assert restored_snapshot is not None
    assert dump_business_snapshot(restored_snapshot) == dump_business_snapshot(final_snapshot)
    assert ProductProjectRepository(restarted_store).get("product-project-1").project_id == (
        "product-project-1"
    )

    restarted = BusinessFactory.restore(restored_snapshot)
    restarted.resolve_support_case(case_id="support-1", resolution_ref="support:resolution:1")
    restarted_business_store.save(
        restarted.snapshot(),
        expected_row_version=restored_snapshot.row_version,
    )


def test_business_flow_fails_closed_before_required_policy_gates() -> None:
    factory = _factory()
    with pytest.raises(BusinessFactoryError, match="outside research package"):
        factory.identify_opportunity(
            opportunity_id="opportunity-1",
            title="Unproven opportunity",
            evidence_ids=("invented-evidence",),
        )

    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Evidence-backed opportunity",
        evidence_ids=("evidence-demand",),
    )
    with pytest.raises(BusinessFactoryError, match="outside business policy"):
        factory.create_lead(
            lead_id="lead-1",
            channel_id="unapproved-social-network",
            counterparty_ref="counterparty:test:1",
        )

    factory.create_lead(
        lead_id="lead-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:1",
    )
    with pytest.raises(BusinessFactoryError, match="qualified lead"):
        factory.draft_proposal(proposal_id="proposal-1", scope_summary="Not qualified")

    factory.qualify_lead(qualification_ref="qualification:review:1")
    factory.draft_proposal(proposal_id="proposal-1", scope_summary="Draft only")
    with pytest.raises(BusinessFactoryError, match="trusted business approval authority"):
        factory.approve_proposal(approval_ref="caller:self-minted-proposal-approval")
    with pytest.raises(BusinessFactoryError, match="approved proposal"):
        factory.create_work_order(
            work_order_id="work-order-1",
            scope="Should remain blocked",
            authorization_ref="approval:work-order:1",
        )


def test_delivery_requires_qa_and_exact_release_grant(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory = _factory(business_authority)
    _advance_to_work_order(factory, business_authority)
    factory.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-project-1",
        project_name="Expense App Test Product",
        spec=ProductProjectSpec(
            goal="Build test product",
            desired_outcome="Verified output",
            compliance={"business_work_order_ref": "work-order-1"},
        ),
        idempotency_key="work-order-1",
    )

    artifact_ref = "artifact:1"
    allowed = _allowed_release_grant(
        "product-project-1",
        artifact_ref=artifact_ref,
        bundle_dir=tmp_path / "release-bundle",
    )
    with pytest.raises(BusinessFactoryError, match="passing QA"):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref=artifact_ref,
            authorization_ref="approval:delivery:1",
            compliance=allowed,
        )

    factory.record_qa(state=QAState.PASSED, evidence_ref="qa:report:1")
    blocked = ProductComplianceDecision(
        project_id="product-project-1",
        allowed=False,
        findings=("license:blocked:component-1",),
        evidence_refs=("review:license:1",),
    )
    with pytest.raises(BusinessFactoryError, match="exact PF10 release compliance grant"):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref=artifact_ref,
            authorization_ref="approval:delivery:1",
            compliance=blocked,  # type: ignore[arg-type]
        )
    with pytest.raises(BusinessFactoryError, match="exact-artifact PF10 release compliance grant"):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref=artifact_ref,
            authorization_ref="approval:delivery:1",
            compliance=replace(allowed, project_id="other-project"),
        )
    with pytest.raises(BusinessFactoryError, match="exact-artifact PF10 release compliance grant"):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref="artifact:substituted",
            authorization_ref="approval:delivery:1",
            compliance=allowed,
        )
    with pytest.raises(BusinessFactoryError, match="trusted business approval authority"):
        factory.record_delivery(
            delivery_id="delivery-1",
            artifact_ref=artifact_ref,
            authorization_ref="caller:self-minted-delivery-approval",
            compliance=allowed,
        )


def test_authorization_fingerprint_detects_scope_substitution(business_authority) -> None:
    factory = _factory(business_authority)
    _advance_to_work_order(factory, business_authority)
    snapshot = factory.snapshot()
    assert snapshot.work_order is not None
    forged_order = replace(snapshot.work_order, scope="Attacker-changed scope")
    forged = replace(snapshot, work_order=forged_order)
    with pytest.raises(BusinessFactoryError, match="fingerprint does not match scope"):
        dump_business_snapshot(forged)


def test_policy_cannot_expand_contract_or_money_authority() -> None:
    with pytest.raises(BusinessFactoryError, match="contract authority"):
        BusinessPolicy(
            policy_id="policy-1",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.DRAFT_ONLY,
            contract_authority="autonomous",  # type: ignore[arg-type]
        )
    with pytest.raises(BusinessFactoryError, match="record-only"):
        BusinessPolicy(
            policy_id="policy-1",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.DRAFT_ONLY,
            financial_authority="execute-payments",  # type: ignore[arg-type]
        )


def test_snapshot_round_trip_rejects_forged_state() -> None:
    factory = _factory()
    payload = dump_business_snapshot(factory.snapshot())
    restored = load_business_snapshot(payload)
    assert dump_business_snapshot(restored) == payload

    forged = replace(restored, row_version=restored.row_version + 1)
    with pytest.raises(BusinessFactoryError, match="row_version/audit mismatch"):
        dump_business_snapshot(forged)


def test_business_repository_rejects_stale_or_non_advancing_writes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()
    factory = _factory()
    first = factory.snapshot()
    repository.save(first, expected_row_version=0)

    with pytest.raises(StaleBusinessStateError, match="advance"):
        repository.save(first, expected_row_version=first.row_version)

    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Evidence-backed opportunity",
        evidence_ids=("evidence-demand",),
    )
    with pytest.raises(StaleBusinessStateError, match="row version changed"):
        repository.save(factory.snapshot(), expected_row_version=0)
