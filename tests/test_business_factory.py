from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.business_authority import BusinessAuthorizationIntent
from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactoryError,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    ContractAuthority,
    FinancialAuthority,
    dump_business_snapshot,
    load_business_snapshot,
)
from nika_core.product_project import EvidenceRef, ProductProjectSpec, ResearchEvidencePackage


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


def _research() -> ResearchEvidencePackage:
    return ResearchEvidencePackage(
        package_id="research-market-1",
        evidence=(
            EvidenceRef(
                evidence_id="evidence-demand",
                provenance_ref="research:source:public:1",
                claim="Demand exists",
            ),
            EvidenceRef(
                evidence_id="evidence-price",
                provenance_ref="research:source:public:2",
                claim="Price evidence exists",
            ),
        ),
        research_artifact_ref="research:artifact:market-1",
    )


def _spec(
    work_order_id: str = "work-order-1",
    *,
    goal: str = "Build the authorized product",
) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A deterministic local product result",
        compliance={"business_work_order_ref": work_order_id},
    )


def _factory(authority=None) -> BusinessFactory:
    return BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-1",
            goal="Validate an evidence-backed product opportunity",
            research_package=_research(),
        ),
        policy=BusinessPolicy(
            policy_id="policy-1",
            allowed_channel_ids=("sandbox-email", "test-marketplace"),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
        approval_authority=authority,
    )


def _advance_to_proposal(factory: BusinessFactory) -> None:
    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Evidence-backed controlled product",
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
        scope_summary="Build the controlled product scope.",
    )


def _advance_to_work_order(
    factory: BusinessFactory,
    authority: _Authority,
    *,
    product_spec: ProductProjectSpec | None = None,
) -> ProductProjectSpec:
    _advance_to_proposal(factory)
    authority.allow("approval:proposal:1")
    factory.approve_proposal(approval_ref="approval:proposal:1")
    spec = product_spec or _spec()
    authority.allow("approval:work-order:1")
    factory.create_work_order(
        work_order_id="work-order-1",
        scope="Implement only the approved product scope.",
        target_project_id="product-project-1",
        target_project_name="Authorized Product",
        product_spec=spec,
        authorization_ref="approval:work-order:1",
    )
    return spec


def test_business_flow_fails_closed_before_required_policy_and_authority_gates() -> None:
    factory = _factory()
    with pytest.raises(BusinessFactoryError, match="outside research package"):
        factory.identify_opportunity(
            opportunity_id="opportunity-1",
            title="Unsupported opportunity",
            evidence_ids=("invented-evidence",),
        )

    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Supported opportunity",
        evidence_ids=("evidence-demand",),
    )
    with pytest.raises(BusinessFactoryError, match="outside business policy"):
        factory.create_lead(
            lead_id="lead-1",
            channel_id="unapproved-network",
            counterparty_ref="counterparty:test:1",
        )

    factory.create_lead(
        lead_id="lead-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:1",
    )
    with pytest.raises(BusinessFactoryError, match="qualified lead"):
        factory.draft_proposal(
            proposal_id="proposal-1",
            scope_summary="Not qualified.",
        )
    factory.qualify_lead(qualification_ref="qualification:review:1")
    factory.draft_proposal(
        proposal_id="proposal-1",
        scope_summary="Draft scope.",
    )
    with pytest.raises(BusinessFactoryError, match="trusted business approval authority"):
        factory.approve_proposal(approval_ref="caller:self-minted-approval")


def test_work_order_requires_exact_product_project_spec_and_target_authority() -> None:
    authority = _Authority()
    factory = _factory(authority)
    _advance_to_proposal(factory)
    authority.allow("approval:proposal:1")
    factory.approve_proposal(approval_ref="approval:proposal:1")

    wrong_ref_spec = _spec("other-work-order")
    authority.allow("approval:work-order:wrong-ref")
    with pytest.raises(BusinessFactoryError, match="exact authorized business WorkOrder"):
        factory.create_work_order(
            work_order_id="work-order-1",
            scope="Approved scope",
            target_project_id="product-project-1",
            target_project_name="Authorized Product",
            product_spec=wrong_ref_spec,
            authorization_ref="approval:work-order:wrong-ref",
        )

    authority.allow("approval:work-order:1")
    order = factory.create_work_order(
        work_order_id="work-order-1",
        scope="Approved scope",
        target_project_id="product-project-1",
        target_project_name="Authorized Product",
        product_spec=_spec(),
        authorization_ref="approval:work-order:1",
    )
    assert order.target_project_id == "product-project-1"
    assert order.target_project_name == "Authorized Product"
    assert len(order.product_spec_fingerprint) == 64


def test_work_order_authorization_detects_target_identity_or_scope_substitution() -> None:
    authority = _Authority()
    factory = _factory(authority)
    _advance_to_work_order(factory, authority)
    snapshot = factory.snapshot()
    assert snapshot.work_order is not None

    forged_target = replace(snapshot.work_order, target_project_id="attacker-project")
    with pytest.raises(BusinessFactoryError, match="fingerprint does not match effect"):
        dump_business_snapshot(replace(snapshot, work_order=forged_target))

    forged_scope = replace(snapshot.work_order, scope="Attacker-changed scope")
    with pytest.raises(BusinessFactoryError, match="fingerprint does not match effect"):
        dump_business_snapshot(replace(snapshot, work_order=forged_scope))


def test_snapshot_round_trip_preserves_exact_pf9_authority_lineage() -> None:
    authority = _Authority()
    factory = _factory(authority)
    _advance_to_work_order(factory, authority)

    payload = dump_business_snapshot(factory.snapshot())
    restored = load_business_snapshot(payload)
    assert dump_business_snapshot(restored) == payload
    assert restored.work_order is not None
    assert restored.work_order.product_project_id is None


def test_snapshot_rejects_authorization_fingerprint_tamper() -> None:
    authority = _Authority()
    factory = _factory(authority)
    _advance_to_work_order(factory, authority)
    snapshot = factory.snapshot()
    assert snapshot.work_order is not None
    forged = replace(
        snapshot,
        work_order=replace(snapshot.work_order, authorization_fingerprint="0" * 64),
    )
    with pytest.raises(BusinessFactoryError, match="fingerprint does not match effect"):
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
            financial_authority="autonomous",  # type: ignore[arg-type]
        )
    safe = BusinessPolicy(
        policy_id="policy-safe",
        allowed_channel_ids=("sandbox-email",),
        communication_authority=CommunicationAuthority.DRAFT_ONLY,
        contract_authority=ContractAuthority.APPROVAL_REQUIRED,
        financial_authority=FinancialAuthority.RECORD_ONLY,
    )
    assert safe.contract_authority is ContractAuthority.APPROVAL_REQUIRED
    assert safe.financial_authority is FinancialAuthority.RECORD_ONLY


def test_caller_cannot_prepopulate_trusted_pf9_lineage() -> None:
    authority = _Authority()
    factory = _factory(authority)
    _advance_to_proposal(factory)
    authority.allow("approval:proposal:1")
    factory.approve_proposal(approval_ref="approval:proposal:1")
    injected = ProductProjectSpec(
        goal="Injected lineage product",
        desired_outcome="Should be rejected",
        compliance={
            "business_work_order_ref": "work-order-1",
            "business_objective_ref": "attacker-objective",
        },
    )
    authority.allow("approval:work-order:1")
    with pytest.raises(BusinessFactoryError, match="cannot pre-populate trusted PF9 lineage"):
        factory.create_work_order(
            work_order_id="work-order-1",
            scope="Approved scope",
            target_project_id="product-project-1",
            target_project_name="Authorized Product",
            product_spec=injected,
            authorization_ref="approval:work-order:1",
        )


def test_business_snapshot_rejects_token_shaped_raw_credential() -> None:
    factory = _factory()
    factory.identify_opportunity(
        opportunity_id="opportunity-1",
        title="Supported opportunity",
        evidence_ids=("evidence-demand",),
    )
    with pytest.raises(BusinessFactoryError, match="raw credential material"):
        factory.create_lead(
            lead_id="lead-1",
            channel_id="sandbox-email",
            counterparty_ref="sk-" + "a" * 24,
        )
