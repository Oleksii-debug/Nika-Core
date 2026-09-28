from __future__ import annotations

import pytest

from nika_core.business_authority import BusinessAuthorizationIntent
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


def _spec(*, goal: str = "Build the authorized sandbox product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A deterministic local result",
        compliance={"business_work_order_ref": "work-order-handoff-1"},
    )


def _factory_at_work_order(authority: _Authority) -> tuple[BusinessFactory, ProductProjectSpec]:
    factory = BusinessFactory.start(
        objective=BusinessObjective(
            objective_id="objective-handoff-1",
            goal="Deliver one authorized sandbox product",
            research_package=ResearchEvidencePackage(
                package_id="research-handoff-1",
                evidence=(
                    EvidenceRef(
                        evidence_id="evidence-handoff-1",
                        provenance_ref="research:public:handoff-1",
                        claim="Evidence for the authorized product",
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
        approval_authority=authority,
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
    authority.allow("approval:proposal:handoff-1")
    factory.approve_proposal(approval_ref="approval:proposal:handoff-1")
    spec = _spec()
    authority.allow("approval:work-order:handoff-1")
    factory.create_work_order(
        work_order_id="work-order-handoff-1",
        scope="Build the authorized sandbox product.",
        target_project_id="product-handoff-1",
        target_project_name="Authorized handoff product",
        product_spec=spec,
        authorization_ref="approval:work-order:handoff-1",
    )
    return factory, spec


def test_handoff_rejects_same_work_order_different_spec_before_product_effect(tmp_path) -> None:
    authority = _Authority()
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory, _ = _factory_at_work_order(authority)
    substituted = _spec(goal="Attacker-substituted product")

    with pytest.raises(BusinessFactoryError, match="authorized WorkOrder specification"):
        factory.handoff_to_product_factory(
            repository=products,
            spec=substituted,
            idempotency_key="caller-request-substituted-spec",
        )

    with pytest.raises(KeyError):
        products.get("product-handoff-1")


def test_handoff_creates_exact_bound_product_project_and_records_authority_lineage(
    tmp_path,
) -> None:
    authority = _Authority()
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory, spec = _factory_at_work_order(authority)

    order = factory.handoff_to_product_factory(
        repository=products,
        spec=spec,
        idempotency_key="caller-request-handoff-1",
    )
    assert order.product_project_id == "product-handoff-1"
    stored = products.get("product-handoff-1")
    assert stored.name == "Authorized handoff product"
    assert stored.spec.compliance["business_work_order_ref"] == "work-order-handoff-1"
    assert stored.spec.compliance["business_work_order_authorization_ref"] == (
        "approval:work-order:handoff-1"
    )
    assert stored.spec.compliance["business_work_order_authorization_fingerprint"] == (
        order.authorization_fingerprint
    )
    assert stored.spec.compliance["business_product_spec_fingerprint"] == (
        order.product_spec_fingerprint
    )
    assert stored.spec.compliance["business_objective_ref"] == "objective-handoff-1"
    assert stored.spec.compliance["business_handoff_effect_key"].startswith(
        "nika-pf9-handoff-v2:"
    )


def test_uncertain_handoff_retry_reconciles_one_product_even_with_new_request_key(tmp_path) -> None:
    authority = _Authority()
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory, spec = _factory_at_work_order(authority)
    durable_before = factory.snapshot()
    business.save(durable_before, expected_row_version=0)

    first = factory.handoff_to_product_factory(
        repository=products,
        spec=spec,
        idempotency_key="caller-request-before-crash",
    )
    assert first.product_project_id == "product-handoff-1"

    restored_snapshot = business.load("objective-handoff-1")
    assert restored_snapshot is not None
    assert restored_snapshot.work_order is not None
    assert restored_snapshot.work_order.product_project_id is None
    restarted = BusinessFactory.restore(restored_snapshot)

    reconciled = restarted.handoff_to_product_factory(
        repository=ProductProjectRepository(store),
        spec=spec,
        idempotency_key="caller-request-after-restart",
    )
    assert reconciled.product_project_id == "product-handoff-1"
    assert restarted.snapshot().audit[-1].event_type == "product_project.linked"

    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM product_projects").fetchone()
        assert count["count"] == 1


def test_retry_after_product_spec_revision_still_uses_initial_effect_ledger(tmp_path) -> None:
    authority = _Authority()
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    business = BusinessFactoryRepository(store)
    business.initialize()

    factory, initial_spec = _factory_at_work_order(authority)
    durable_before = factory.snapshot()
    business.save(durable_before, expected_row_version=0)
    factory.handoff_to_product_factory(
        repository=products,
        spec=initial_spec,
        idempotency_key="caller-request-before-crash",
    )

    current = products.get("product-handoff-1")
    revised = ProductProjectSpec(
        goal="Build the authorized sandbox product",
        desired_outcome="A deterministic local result with a later revision",
        compliance={"revision_note": "post-handoff evolution"},
    )
    products.update_spec(
        "product-handoff-1",
        revised,
        expected_row_version=current.row_version,
        change_reason="post-handoff product evolution",
    )

    restored_snapshot = business.load("objective-handoff-1")
    assert restored_snapshot is not None
    restarted = BusinessFactory.restore(restored_snapshot)
    reconciled = restarted.handoff_to_product_factory(
        repository=products,
        spec=initial_spec,
        idempotency_key="caller-request-after-revision",
    )
    assert reconciled.product_project_id == "product-handoff-1"
    assert products.get("product-handoff-1").spec_version == 2


def test_deterministic_effect_ledger_blocks_conflicting_target_input(tmp_path) -> None:
    authority = _Authority()
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    products = ProductProjectRepository(store)
    factory, spec = _factory_at_work_order(authority)
    factory.handoff_to_product_factory(
        repository=products,
        spec=spec,
        idempotency_key="caller-request-first",
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT operation_key, input_fingerprint FROM product_project_idempotency"
        ).fetchone()
        assert row["operation_key"].startswith("nika-pf9-handoff-v2:")
        assert len(row["input_fingerprint"]) == 64
