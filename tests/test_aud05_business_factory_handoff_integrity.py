from __future__ import annotations

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactoryError,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
)
from nika_core.business_factory_persistence import BusinessFactoryRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)


def _factory_at_work_order() -> BusinessFactory:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-aud05",
            goal="Deliver the authorized expense application",
            research_package=ResearchEvidencePackage(
                package_id="research-aud05",
                evidence=(
                    EvidenceRef(
                        "evidence-aud05",
                        "research:public:aud05",
                        "Expense workflow demand",
                    ),
                ),
                research_artifact_ref="research:result:aud05",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-aud05",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-aud05",
        title="Authorized expense application",
        evidence_ids=("evidence-aud05",),
    )
    factory.create_lead(
        lead_id="lead-aud05",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:aud05",
    )
    factory.qualify_lead(qualification_ref="qualification:aud05")
    factory.draft_proposal(
        proposal_id="proposal-aud05",
        scope_summary="Build the authorized expense application.",
    )
    factory.approve_proposal(approval_ref="approval:proposal:aud05")
    factory.create_work_order(
        work_order_id="work-order-aud05",
        scope="Build the authorized expense application.",
        authorization_ref="approval:work-order:aud05",
    )
    return factory


def _spec(
    *,
    work_order_ref: str,
    goal: str = "Build the authorized expense application",
) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A QA-reviewed release candidate",
        compliance={"business_work_order_ref": work_order_ref},
    )


def test_pf9_handoff_rejects_product_spec_bound_to_another_work_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    factory = _factory_at_work_order()

    with pytest.raises(BusinessFactoryError):
        factory.handoff_to_product_factory(
            repository=ProductProjectRepository(store),
            project_id="product-aud05-wrong-authority",
            project_name="Wrong authority product",
            spec=_spec(
                work_order_ref="different-work-order",
                goal="Build a different unauthorized product",
            ),
            idempotency_key="aud05-wrong-work-order-binding",
        )


def test_pf9_restart_cannot_create_second_product_after_uncertain_handoff(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory = _factory_at_work_order()
    durable_before_handoff = factory.snapshot()
    business.save(durable_before_handoff, expected_row_version=0)

    first = factory.handoff_to_product_factory(
        repository=products,
        project_id="product-aud05-first",
        project_name="Authorized product first attempt",
        spec=_spec(work_order_ref="work-order-aud05"),
        idempotency_key="aud05-handoff-first",
    )
    assert first.product_project_id == "product-aud05-first"
    assert products.get("product-aud05-first").project_id == "product-aud05-first"

    # Simulate process loss after ProductProjectRepository.create() committed but before the
    # updated PF9 aggregate was persisted. Durable PF9 authority still says the WorkOrder is
    # unlinked even though the first ProductProject side effect already exists.
    restored_snapshot = business.load("objective-aud05")
    assert restored_snapshot is not None
    assert restored_snapshot.work_order is not None
    assert restored_snapshot.work_order.product_project_id is None
    restarted = BusinessFactory.restore(restored_snapshot)

    # A retry must reconcile the first durable product effect or fail closed. It must not allow
    # caller-controlled new identity/idempotency input to create a second ProductProject for the
    # same authorized WorkOrder.
    with pytest.raises(BusinessFactoryError):
        restarted.handoff_to_product_factory(
            repository=products,
            project_id="product-aud05-second",
            project_name="Unauthorized duplicate after restart",
            spec=_spec(work_order_ref="work-order-aud05"),
            idempotency_key="aud05-handoff-second",
        )
