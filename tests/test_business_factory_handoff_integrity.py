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
            objective_id="objective-handoff-1",
            goal="Deliver one authorized sandbox product",
            research_package=ResearchEvidencePackage(
                package_id="research-handoff-1",
                evidence=(
                    EvidenceRef(
                        "evidence-handoff-1",
                        "research:public:handoff-1",
                        "Evidence for the authorized product",
                    ),
                ),
                research_artifact_ref="research:artifact:handoff-1",
            ),
        ),
        policy=BusinessPolicy(
            policy_id="policy-handoff-1",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.APPROVAL_REQUIRED,
        ),
    )
    factory.identify_opportunity(
        opportunity_id="opportunity-handoff-1",
        title="Authorized sandbox product",
        evidence_ids=("evidence-handoff-1",),
    )
    factory.create_lead(
        lead_id="lead-handoff-1",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:test:handoff-1",
    )
    factory.qualify_lead(qualification_ref="qualification:handoff-1")
    factory.draft_proposal(
        proposal_id="proposal-handoff-1",
        scope_summary="Build the authorized sandbox product.",
    )
    factory.approve_proposal(approval_ref="approval:proposal:handoff-1")
    factory.create_work_order(
        work_order_id="work-order-handoff-1",
        scope="Build the authorized sandbox product.",
        authorization_ref="approval:work-order:handoff-1",
    )
    return factory


def _spec(work_order_ref: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build the authorized sandbox product",
        desired_outcome="A QA-reviewed sandbox release candidate",
        compliance={"business_work_order_ref": work_order_ref},
    )


def test_handoff_rejects_spec_bound_to_another_work_order_before_product_effect(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory = _factory_at_work_order()

    with pytest.raises(BusinessFactoryError, match="exact authorized business WorkOrder"):
        factory.handoff_to_product_factory(
            repository=products,
            project_id="product-wrong-work-order",
            project_name="Wrong WorkOrder product",
            spec=_spec("work-order-other"),
            idempotency_key="caller-request-wrong-work-order",
        )

    with pytest.raises(KeyError):
        products.get("product-wrong-work-order")


def test_uncertain_handoff_exact_retry_reconciles_existing_product(tmp_path) -> None:
    database_path = tmp_path / "nika.sqlite"
    store = SQLiteStore(database_path)
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory = _factory_at_work_order()
    durable_before = factory.snapshot()
    business.save(durable_before, expected_row_version=0)

    first = factory.handoff_to_product_factory(
        repository=products,
        project_id="product-handoff-1",
        project_name="Authorized handoff product",
        spec=_spec("work-order-handoff-1"),
        idempotency_key="caller-request-handoff-1",
    )
    assert first.product_project_id == "product-handoff-1"
    stored = products.get("product-handoff-1")
    assert stored.spec.compliance["business_work_order_ref"] == "work-order-handoff-1"
    assert stored.spec.compliance["business_work_order_authorization_ref"] == (
        "approval:work-order:handoff-1"
    )
    assert stored.spec.compliance["business_objective_ref"] == "objective-handoff-1"

    restored_snapshot = business.load("objective-handoff-1")
    assert restored_snapshot is not None
    assert restored_snapshot.work_order is not None
    assert restored_snapshot.work_order.product_project_id is None
    restarted = BusinessFactory.restore(restored_snapshot)

    reconciled = restarted.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        project_id="product-handoff-1",
        project_name="Authorized handoff product",
        spec=_spec("work-order-handoff-1"),
        idempotency_key="caller-request-handoff-1",
    )
    assert reconciled.product_project_id == "product-handoff-1"
    assert restarted.snapshot().audit[-1].event_type == "product_project.linked"


def test_uncertain_handoff_cannot_create_second_product_for_same_work_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory = _factory_at_work_order()
    durable_before = factory.snapshot()
    business.save(durable_before, expected_row_version=0)
    factory.handoff_to_product_factory(
        repository=products,
        project_id="product-handoff-first",
        project_name="First authorized product effect",
        spec=_spec("work-order-handoff-1"),
        idempotency_key="caller-request-first",
    )

    restored_snapshot = business.load("objective-handoff-1")
    assert restored_snapshot is not None
    restarted = BusinessFactory.restore(restored_snapshot)
    with pytest.raises(BusinessFactoryError, match="durable WorkOrder effect"):
        restarted.handoff_to_product_factory(
            repository=products,
            project_id="product-handoff-second",
            project_name="Conflicting duplicate product effect",
            spec=_spec("work-order-handoff-1"),
            idempotency_key="caller-request-second",
        )

    assert products.get("product-handoff-first").project_id == "product-handoff-first"
    with pytest.raises(KeyError):
        products.get("product-handoff-second")
