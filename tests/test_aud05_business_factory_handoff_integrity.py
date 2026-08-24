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


def _factory_at_work_order(business_authority) -> BusinessFactory:
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
        approval_authority=business_authority,
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
    business_authority.allow_once("approval:proposal:aud05")
    factory.approve_proposal(approval_ref="approval:proposal:aud05")
    business_authority.allow_once("approval:work-order:aud05")
    factory.create_work_order(
        work_order_id="work-order-aud05",
        scope="Build the authorized expense application.",
        authorization_ref="approval:work-order:aud05",
        product_spec=_spec(work_order_ref="work-order-aud05"),
    )
    return factory


def test_pf9_handoff_rejects_product_spec_bound_to_another_work_order(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory = _factory_at_work_order(business_authority)

    with pytest.raises(BusinessFactoryError):
        factory.handoff_to_product_factory(
            repository=products,
            project_id="product-aud05-wrong-authority",
            project_name="Wrong authority product",
            spec=_spec(
                work_order_ref="different-work-order",
                goal="Build a different unauthorized product",
            ),
            idempotency_key="aud05-wrong-work-order-binding",
        )

    with pytest.raises(KeyError):
        products.get("product-aud05-wrong-authority")


def test_pf9_handoff_rejects_same_work_order_with_substituted_product_spec_before_effect(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory = _factory_at_work_order(business_authority)

    with pytest.raises(BusinessFactoryError, match="authorized WorkOrder specification"):
        factory.handoff_to_product_factory(
            repository=products,
            project_id="product-aud05-substituted-spec",
            project_name="Same WorkOrder substituted product",
            spec=_spec(
                work_order_ref="work-order-aud05",
                goal="Build a different unauthorized product under the same WorkOrder id",
            ),
            idempotency_key="aud05-same-work-order-substituted-spec",
        )

    with pytest.raises(KeyError):
        products.get("product-aud05-substituted-spec")


def test_pf9_restart_cannot_create_second_product_after_uncertain_handoff(
    tmp_path,
    business_authority,
) -> None:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory = _factory_at_work_order(business_authority)
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

    restored_snapshot = business.load("objective-aud05")
    assert restored_snapshot is not None
    assert restored_snapshot.work_order is not None
    assert restored_snapshot.work_order.product_project_id is None
    restarted = BusinessFactory.restore(restored_snapshot)

    with pytest.raises(BusinessFactoryError):
        restarted.handoff_to_product_factory(
            repository=products,
            project_id="product-aud05-second",
            project_name="Unauthorized duplicate after restart",
            spec=_spec(work_order_ref="work-order-aud05"),
            idempotency_key="aud05-handoff-second",
        )

    assert products.get("product-aud05-first").project_id == "product-aud05-first"
    with pytest.raises(KeyError):
        products.get("product-aud05-second")
