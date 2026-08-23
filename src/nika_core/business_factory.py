from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.product_compliance import ProductComplianceDecision
from nika_core.product_project import (
    EvidenceRef,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)

BUSINESS_FACTORY_SCHEMA = "nika.business_factory.v1"


class BusinessFactoryError(ValueError):
    pass


class StaleBusinessStateError(BusinessFactoryError):
    pass


class CommunicationAuthority(StrEnum):
    DRAFT_ONLY = "draft_only"
    APPROVAL_REQUIRED = "approval_required"
    STANDING_POLICY = "standing_policy"


class ContractAuthority(StrEnum):
    APPROVAL_REQUIRED = "approval_required"


class FinancialAuthority(StrEnum):
    RECORD_ONLY = "record_only"


class ProposalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class QAState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class InvoicePaymentState(StrEnum):
    UNBILLED = "unbilled"
    INVOICED = "invoiced"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class SupportCaseState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class BusinessPolicy:
    policy_id: str
    allowed_channel_ids: tuple[str, ...]
    communication_authority: CommunicationAuthority
    contract_authority: ContractAuthority = ContractAuthority.APPROVAL_REQUIRED
    financial_authority: FinancialAuthority = FinancialAuthority.RECORD_ONLY
    standing_policy_ref: str | None = None

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        _unique(self.allowed_channel_ids, "allowed channel id")
        if not self.allowed_channel_ids:
            raise BusinessFactoryError("business policy requires at least one allowed channel")
        if not isinstance(self.communication_authority, CommunicationAuthority):
            raise BusinessFactoryError("communication_authority is invalid")
        if self.contract_authority is not ContractAuthority.APPROVAL_REQUIRED:
            raise BusinessFactoryError("external contract authority cannot become autonomous")
        if self.financial_authority is not FinancialAuthority.RECORD_ONLY:
            raise BusinessFactoryError("financial authority is record-only in Business Factory")
        if self.communication_authority is CommunicationAuthority.STANDING_POLICY:
            if self.standing_policy_ref is None:
                raise BusinessFactoryError(
                    "standing communication policy requires standing_policy_ref"
                )
            _text(self.standing_policy_ref, "standing_policy_ref")
        elif self.standing_policy_ref is not None:
            raise BusinessFactoryError("standing_policy_ref requires standing-policy authority")


@dataclass(frozen=True, slots=True)
class BusinessObjective:
    objective_id: str
    goal: str
    research_package: ResearchEvidencePackage

    def __post_init__(self) -> None:
        _text(self.objective_id, "objective_id")
        _text(self.goal, "business goal")
        if not isinstance(self.research_package, ResearchEvidencePackage):
            raise BusinessFactoryError("objective research must use ResearchEvidencePackage")


@dataclass(frozen=True, slots=True)
class MarketOpportunity:
    opportunity_id: str
    objective_id: str
    title: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.opportunity_id, "opportunity_id")
        _text(self.objective_id, "opportunity objective_id")
        _text(self.title, "opportunity title")
        _unique(self.evidence_ids, "opportunity evidence id")
        if not self.evidence_ids:
            raise BusinessFactoryError("opportunity requires research evidence")


@dataclass(frozen=True, slots=True)
class BusinessLead:
    lead_id: str
    opportunity_id: str
    channel_id: str
    counterparty_ref: str
    qualification_ref: str | None = None

    @property
    def qualified(self) -> bool:
        return self.qualification_ref is not None


@dataclass(frozen=True, slots=True)
class BusinessProposal:
    proposal_id: str
    lead_id: str
    scope_summary: str
    state: ProposalState = ProposalState.DRAFT
    approval_ref: str | None = None


@dataclass(frozen=True, slots=True)
class BusinessWorkOrder:
    work_order_id: str
    proposal_id: str
    scope: str
    authorization_ref: str
    product_project_id: str | None = None


@dataclass(frozen=True, slots=True)
class QARecord:
    project_id: str
    state: QAState
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    project_id: str
    artifact_ref: str
    authorization_ref: str
    compliance_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    project_id: str
    invoice_ref: str
    state: InvoicePaymentState
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class SupportCase:
    case_id: str
    project_id: str
    summary: str
    evidence_ref: str
    state: SupportCaseState = SupportCaseState.OPEN
    resolution_ref: str | None = None


@dataclass(frozen=True, slots=True)
class BusinessAuditEvent:
    sequence: int
    event_type: str
    subject_id: str
    evidence_ref: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BusinessFactorySnapshot:
    schema: str
    objective: BusinessObjective
    policy: BusinessPolicy
    opportunity: MarketOpportunity | None = None
    lead: BusinessLead | None = None
    proposal: BusinessProposal | None = None
    work_order: BusinessWorkOrder | None = None
    qa: QARecord | None = None
    delivery: DeliveryRecord | None = None
    payment: PaymentRecord | None = None
    support_cases: tuple[SupportCase, ...] = ()
    audit: tuple[BusinessAuditEvent, ...] = ()
    row_version: int = 0


class BusinessFactory:
    """PF9 lifecycle coordinator with no external-send, contract-signing or money executor."""

    def __init__(self, snapshot: BusinessFactorySnapshot) -> None:
        _validate_snapshot(snapshot)
        self._snapshot = snapshot

    @classmethod
    def start(cls, *, objective: BusinessObjective, policy: BusinessPolicy) -> BusinessFactory:
        snapshot = BusinessFactorySnapshot(
            schema=BUSINESS_FACTORY_SCHEMA,
            objective=objective,
            policy=policy,
        )
        factory = cls(snapshot)
        factory._record(
            "objective.created",
            objective.objective_id,
            objective.research_package.package_id,
        )
        return factory

    @classmethod
    def restore(cls, snapshot: BusinessFactorySnapshot) -> BusinessFactory:
        return cls(snapshot)

    def snapshot(self) -> BusinessFactorySnapshot:
        return self._snapshot

    def identify_opportunity(
        self,
        *,
        opportunity_id: str,
        title: str,
        evidence_ids: tuple[str, ...],
    ) -> MarketOpportunity:
        if self._snapshot.opportunity is not None:
            raise BusinessFactoryError("market opportunity already exists")
        known = {item.evidence_id for item in self._snapshot.objective.research_package.evidence}
        if not set(evidence_ids).issubset(known):
            raise BusinessFactoryError("opportunity references evidence outside research package")
        opportunity = MarketOpportunity(
            opportunity_id=opportunity_id,
            objective_id=self._snapshot.objective.objective_id,
            title=title,
            evidence_ids=evidence_ids,
        )
        self._snapshot = replace(self._snapshot, opportunity=opportunity)
        self._record(
            "opportunity.created",
            opportunity_id,
            self._snapshot.objective.research_package.package_id,
        )
        return opportunity

    def create_lead(
        self,
        *,
        lead_id: str,
        channel_id: str,
        counterparty_ref: str,
    ) -> BusinessLead:
        opportunity = self._require_opportunity()
        if self._snapshot.lead is not None:
            raise BusinessFactoryError("lead already exists")
        _text(lead_id, "lead_id")
        _text(channel_id, "channel_id")
        _text(counterparty_ref, "counterparty_ref")
        if channel_id not in self._snapshot.policy.allowed_channel_ids:
            raise BusinessFactoryError("lead channel is outside business policy")
        lead = BusinessLead(lead_id, opportunity.opportunity_id, channel_id, counterparty_ref)
        self._snapshot = replace(self._snapshot, lead=lead)
        self._record("lead.created", lead_id, channel_id)
        return lead

    def qualify_lead(self, *, qualification_ref: str) -> BusinessLead:
        lead = self._require_lead()
        _text(qualification_ref, "qualification_ref")
        if lead.qualified:
            raise BusinessFactoryError("lead is already qualified")
        lead = replace(lead, qualification_ref=qualification_ref)
        self._snapshot = replace(self._snapshot, lead=lead)
        self._record("lead.qualified", lead.lead_id, qualification_ref)
        return lead

    def draft_proposal(
        self,
        *,
        proposal_id: str,
        scope_summary: str,
    ) -> BusinessProposal:
        lead = self._require_lead()
        if not lead.qualified:
            raise BusinessFactoryError("proposal requires a qualified lead")
        if self._snapshot.proposal is not None:
            raise BusinessFactoryError("proposal already exists")
        _text(proposal_id, "proposal_id")
        _text(scope_summary, "proposal scope_summary")
        proposal = BusinessProposal(proposal_id, lead.lead_id, scope_summary)
        self._snapshot = replace(self._snapshot, proposal=proposal)
        self._record("proposal.drafted", proposal_id, lead.qualification_ref or "qualification")
        return proposal

    def approve_proposal(self, *, approval_ref: str) -> BusinessProposal:
        proposal = self._require_proposal()
        _text(approval_ref, "proposal approval_ref")
        if proposal.state is not ProposalState.DRAFT:
            raise BusinessFactoryError("only a draft proposal can be approved")
        proposal = replace(proposal, state=ProposalState.APPROVED, approval_ref=approval_ref)
        self._snapshot = replace(self._snapshot, proposal=proposal)
        self._record("proposal.approved", proposal.proposal_id, approval_ref)
        return proposal

    def reject_proposal(self, *, rejection_ref: str) -> BusinessProposal:
        proposal = self._require_proposal()
        _text(rejection_ref, "proposal rejection_ref")
        if proposal.state is not ProposalState.DRAFT:
            raise BusinessFactoryError("only a draft proposal can be rejected")
        proposal = replace(proposal, state=ProposalState.REJECTED, approval_ref=rejection_ref)
        self._snapshot = replace(self._snapshot, proposal=proposal)
        self._record("proposal.rejected", proposal.proposal_id, rejection_ref)
        return proposal

    def create_work_order(
        self,
        *,
        work_order_id: str,
        scope: str,
        authorization_ref: str,
    ) -> BusinessWorkOrder:
        proposal = self._require_proposal()
        if proposal.state is not ProposalState.APPROVED:
            raise BusinessFactoryError("work order requires an approved proposal")
        if self._snapshot.work_order is not None:
            raise BusinessFactoryError("work order already exists")
        _text(work_order_id, "work_order_id")
        _text(scope, "work order scope")
        _text(authorization_ref, "work order authorization_ref")
        order = BusinessWorkOrder(work_order_id, proposal.proposal_id, scope, authorization_ref)
        self._snapshot = replace(self._snapshot, work_order=order)
        self._record("work_order.authorized", work_order_id, authorization_ref)
        return order

    def handoff_to_product_factory(
        self,
        *,
        repository: ProductProjectRepository,
        project_id: str,
        project_name: str,
        spec: ProductProjectSpec,
        idempotency_key: str,
    ) -> BusinessWorkOrder:
        order = self._require_work_order()
        _text(project_id, "ProductProject project_id")
        _text(project_name, "ProductProject project_name")
        _text(idempotency_key, "ProductProject handoff request key")
        if not isinstance(spec, ProductProjectSpec):
            raise BusinessFactoryError("ProductProject handoff requires ProductProjectSpec")
        work_order_ref = spec.compliance.get("business_work_order_ref")
        if work_order_ref != order.work_order_id:
            raise BusinessFactoryError(
                "ProductProject spec must bind the exact authorized business WorkOrder"
            )
        if order.product_project_id is not None:
            if order.product_project_id != project_id:
                raise BusinessFactoryError(
                    "work order is already linked to a different ProductProject"
                )
            try:
                linked = repository.get(project_id)
            except KeyError as exc:
                raise BusinessFactoryError(
                    "linked ProductProject is missing from durable repository"
                ) from exc
            if linked.spec.compliance.get("business_work_order_ref") != order.work_order_id:
                raise BusinessFactoryError(
                    "linked ProductProject does not match the authorized business WorkOrder"
                )
            return order

        objective_id = self._snapshot.objective.objective_id
        identity_payload = json.dumps(
            {"objective_id": objective_id, "work_order_id": order.work_order_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_key = "nika-pf9-handoff-v1:" + hashlib.sha256(
            identity_payload.encode("utf-8")
        ).hexdigest()
        compliance = dict(spec.compliance)
        compliance.update(
            {
                "business_work_order_ref": order.work_order_id,
                "business_work_order_authorization_ref": order.authorization_ref,
                "business_objective_ref": objective_id,
                "business_handoff_request_key": idempotency_key,
            }
        )
        bound_spec = replace(spec, compliance=compliance)
        try:
            project = repository.create(
                project_id=project_id,
                name=project_name,
                spec=bound_spec,
                idempotency_key=operation_key,
            )
        except ProductProjectError as exc:
            raise BusinessFactoryError(
                "ProductProject handoff conflicts with durable WorkOrder effect"
            ) from exc
        if project.spec.compliance.get("business_work_order_ref") != order.work_order_id:
            raise BusinessFactoryError(
                "durable ProductProject does not match the authorized business WorkOrder"
            )
        order = replace(order, product_project_id=project.project_id)
        self._snapshot = replace(self._snapshot, work_order=order)
        self._record("product_project.linked", project.project_id, order.authorization_ref)
        return order

    def record_qa(self, *, state: QAState, evidence_ref: str) -> QARecord:
        order = self._require_linked_work_order()
        _text(evidence_ref, "QA evidence_ref")
        if not isinstance(state, QAState):
            raise BusinessFactoryError("QA state is invalid")
        qa = QARecord(order.product_project_id or "", state, evidence_ref)
        self._snapshot = replace(self._snapshot, qa=qa)
        self._record("qa.recorded", qa.project_id, evidence_ref)
        return qa

    def record_delivery(
        self,
        *,
        delivery_id: str,
        artifact_ref: str,
        authorization_ref: str,
        compliance: ProductComplianceDecision,
    ) -> DeliveryRecord:
        order = self._require_linked_work_order()
        qa = self._snapshot.qa
        if qa is None or qa.state is not QAState.PASSED:
            raise BusinessFactoryError("delivery requires passing QA evidence")
        project_id = order.product_project_id or ""
        if compliance.project_id != project_id or not compliance.allowed:
            raise BusinessFactoryError("delivery requires an allowed PF10 compliance decision")
        _text(delivery_id, "delivery_id")
        _text(artifact_ref, "delivery artifact_ref")
        _text(authorization_ref, "delivery authorization_ref")
        delivery = DeliveryRecord(
            delivery_id,
            project_id,
            artifact_ref,
            authorization_ref,
            compliance.evidence_refs,
        )
        self._snapshot = replace(self._snapshot, delivery=delivery)
        self._record("delivery.authorized", delivery_id, authorization_ref)
        return delivery

    def record_payment_state(
        self,
        *,
        invoice_ref: str,
        state: InvoicePaymentState,
        evidence_ref: str,
    ) -> PaymentRecord:
        delivery = self._require_delivery()
        _text(invoice_ref, "invoice_ref")
        _text(evidence_ref, "payment evidence_ref")
        if not isinstance(state, InvoicePaymentState):
            raise BusinessFactoryError("invoice/payment state is invalid")
        payment = PaymentRecord(delivery.project_id, invoice_ref, state, evidence_ref)
        self._snapshot = replace(self._snapshot, payment=payment)
        self._record("payment_state.recorded", invoice_ref, evidence_ref)
        return payment

    def open_support_case(
        self,
        *,
        case_id: str,
        summary: str,
        evidence_ref: str,
    ) -> SupportCase:
        delivery = self._require_delivery()
        _text(case_id, "support case_id")
        _text(summary, "support summary")
        _text(evidence_ref, "support evidence_ref")
        if any(item.case_id == case_id for item in self._snapshot.support_cases):
            raise BusinessFactoryError("duplicate support case_id")
        case = SupportCase(case_id, delivery.project_id, summary, evidence_ref)
        self._snapshot = replace(
            self._snapshot,
            support_cases=(*self._snapshot.support_cases, case),
        )
        self._record("support.opened", case_id, evidence_ref)
        return case

    def resolve_support_case(self, *, case_id: str, resolution_ref: str) -> SupportCase:
        _text(case_id, "support case_id")
        _text(resolution_ref, "support resolution_ref")
        cases = list(self._snapshot.support_cases)
        for index, case in enumerate(cases):
            if case.case_id != case_id:
                continue
            if case.state is SupportCaseState.RESOLVED:
                raise BusinessFactoryError("support case is already resolved")
            updated = replace(
                case,
                state=SupportCaseState.RESOLVED,
                resolution_ref=resolution_ref,
            )
            cases[index] = updated
            self._snapshot = replace(self._snapshot, support_cases=tuple(cases))
            self._record("support.resolved", case_id, resolution_ref)
            return updated
        raise BusinessFactoryError("unknown support case_id")

    def _record(self, event_type: str, subject_id: str, evidence_ref: str) -> None:
        event = BusinessAuditEvent(
            sequence=len(self._snapshot.audit) + 1,
            event_type=event_type,
            subject_id=subject_id,
            evidence_ref=evidence_ref,
            recorded_at=datetime.now(UTC).isoformat(),
        )
        self._snapshot = replace(
            self._snapshot,
            audit=(*self._snapshot.audit, event),
            row_version=self._snapshot.row_version + 1,
        )

    def _require_opportunity(self) -> MarketOpportunity:
        if self._snapshot.opportunity is None:
            raise BusinessFactoryError("market opportunity is required")
        return self._snapshot.opportunity

    def _require_lead(self) -> BusinessLead:
        if self._snapshot.lead is None:
            raise BusinessFactoryError("lead is required")
        return self._snapshot.lead

    def _require_proposal(self) -> BusinessProposal:
        if self._snapshot.proposal is None:
            raise BusinessFactoryError("proposal is required")
        return self._snapshot.proposal

    def _require_work_order(self) -> BusinessWorkOrder:
        if self._snapshot.work_order is None:
            raise BusinessFactoryError("work order is required")
        return self._snapshot.work_order

    def _require_linked_work_order(self) -> BusinessWorkOrder:
        order = self._require_work_order()
        if order.product_project_id is None:
            raise BusinessFactoryError("work order must be linked to ProductProject")
        return order

    def _require_delivery(self) -> DeliveryRecord:
        if self._snapshot.delivery is None:
            raise BusinessFactoryError("delivery is required")
        return self._snapshot.delivery


def dump_business_snapshot(snapshot: BusinessFactorySnapshot) -> str:
    _validate_snapshot(snapshot)
    return json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_business_snapshot(payload: str) -> BusinessFactorySnapshot:
    if not isinstance(payload, str) or not payload.strip():
        raise BusinessFactoryError("business snapshot must be non-empty JSON text")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BusinessFactoryError("business snapshot is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise BusinessFactoryError("business snapshot root must be an object")
    expected = {
        "schema",
        "objective",
        "policy",
        "opportunity",
        "lead",
        "proposal",
        "work_order",
        "qa",
        "delivery",
        "payment",
        "support_cases",
        "audit",
        "row_version",
    }
    if set(raw) != expected:
        raise BusinessFactoryError("business snapshot fields do not match schema")
    objective_raw = _mapping(raw["objective"], "objective")
    package_raw = _mapping(objective_raw["research_package"], "research package")
    evidence = tuple(
        EvidenceRef(
            evidence_id=_value(item, "evidence_id", "research evidence"),
            provenance_ref=_value(item, "provenance_ref", "research evidence"),
            claim=_optional_value(item, "claim", "research evidence") or "",
        )
        for item in _items(package_raw, "evidence", "research package")
    )
    objective = BusinessObjective(
        objective_id=_value(objective_raw, "objective_id", "objective"),
        goal=_value(objective_raw, "goal", "objective"),
        research_package=ResearchEvidencePackage(
            package_id=_value(package_raw, "package_id", "research package"),
            evidence=evidence,
            research_artifact_ref=_optional_value(
                package_raw,
                "research_artifact_ref",
                "research package",
            ),
        ),
    )
    policy_raw = _mapping(raw["policy"], "policy")
    policy = BusinessPolicy(
        policy_id=_value(policy_raw, "policy_id", "policy"),
        allowed_channel_ids=_strings(policy_raw, "allowed_channel_ids", "policy"),
        communication_authority=_enum(
            CommunicationAuthority,
            policy_raw.get("communication_authority"),
            "communication authority",
        ),
        contract_authority=_enum(
            ContractAuthority,
            policy_raw.get("contract_authority"),
            "contract authority",
        ),
        financial_authority=_enum(
            FinancialAuthority,
            policy_raw.get("financial_authority"),
            "financial authority",
        ),
        standing_policy_ref=_optional_value(policy_raw, "standing_policy_ref", "policy"),
    )
    snapshot = BusinessFactorySnapshot(
        schema=_value(raw, "schema", "snapshot"),
        objective=objective,
        policy=policy,
        opportunity=_opportunity(raw["opportunity"]),
        lead=_lead(raw["lead"]),
        proposal=_proposal(raw["proposal"]),
        work_order=_work_order(raw["work_order"]),
        qa=_qa(raw["qa"]),
        delivery=_delivery(raw["delivery"]),
        payment=_payment(raw["payment"]),
        support_cases=tuple(
            _support(item) for item in _list(raw["support_cases"], "support cases")
        ),
        audit=tuple(_audit(item) for item in _list(raw["audit"], "audit")),
        row_version=_int(raw["row_version"], "row_version"),
    )
    _validate_snapshot(snapshot)
    return snapshot


def _validate_snapshot(snapshot: BusinessFactorySnapshot) -> None:
    if snapshot.schema != BUSINESS_FACTORY_SCHEMA:
        raise BusinessFactoryError("unsupported business snapshot schema")
    if snapshot.row_version < 0 or snapshot.row_version != len(snapshot.audit):
        raise BusinessFactoryError("business snapshot row_version/audit mismatch")
    for index, event in enumerate(snapshot.audit, start=1):
        if event.sequence != index:
            raise BusinessFactoryError("business audit sequence is not contiguous")
        _text(event.event_type, "audit event_type")
        _text(event.subject_id, "audit subject_id")
        _text(event.evidence_ref, "audit evidence_ref")
        _text(event.recorded_at, "audit recorded_at")

    opportunity = snapshot.opportunity
    if opportunity is None:
        downstream = (
            snapshot.lead,
            snapshot.proposal,
            snapshot.work_order,
            snapshot.qa,
            snapshot.delivery,
            snapshot.payment,
        )
        if any(item is not None for item in downstream) or snapshot.support_cases:
            raise BusinessFactoryError("downstream business state exists without opportunity")
        return
    if opportunity.objective_id != snapshot.objective.objective_id:
        raise BusinessFactoryError("opportunity/objective identity mismatch")
    known_evidence = {item.evidence_id for item in snapshot.objective.research_package.evidence}
    if not set(opportunity.evidence_ids).issubset(known_evidence):
        raise BusinessFactoryError("opportunity contains unknown research evidence")

    lead = snapshot.lead
    if lead is None:
        downstream = (
            snapshot.proposal,
            snapshot.work_order,
            snapshot.qa,
            snapshot.delivery,
            snapshot.payment,
        )
        if any(item is not None for item in downstream) or snapshot.support_cases:
            raise BusinessFactoryError("downstream business state exists without lead")
        return
    _text(lead.lead_id, "lead_id")
    _text(lead.counterparty_ref, "counterparty_ref")
    if lead.opportunity_id != opportunity.opportunity_id:
        raise BusinessFactoryError("lead/opportunity identity mismatch")
    if lead.channel_id not in snapshot.policy.allowed_channel_ids:
        raise BusinessFactoryError("restored lead channel is outside business policy")
    if lead.qualification_ref is not None:
        _text(lead.qualification_ref, "qualification_ref")

    proposal = snapshot.proposal
    if proposal is None:
        downstream = (snapshot.work_order, snapshot.qa, snapshot.delivery, snapshot.payment)
        if any(item is not None for item in downstream) or snapshot.support_cases:
            raise BusinessFactoryError("downstream business state exists without proposal")
        return
    _text(proposal.proposal_id, "proposal_id")
    _text(proposal.scope_summary, "proposal scope_summary")
    if proposal.lead_id != lead.lead_id or not lead.qualified:
        raise BusinessFactoryError("proposal requires the qualified restored lead")
    if proposal.state is ProposalState.APPROVED and not proposal.approval_ref:
        raise BusinessFactoryError("approved proposal is missing approval_ref")
    if proposal.approval_ref is not None:
        _text(proposal.approval_ref, "proposal approval_ref")

    order = snapshot.work_order
    if order is None:
        downstream = (snapshot.qa, snapshot.delivery, snapshot.payment)
        if any(item is not None for item in downstream) or snapshot.support_cases:
            raise BusinessFactoryError("downstream business state exists without work order")
        return
    if proposal.state is not ProposalState.APPROVED or order.proposal_id != proposal.proposal_id:
        raise BusinessFactoryError("work order requires the approved restored proposal")
    _text(order.work_order_id, "work_order_id")
    _text(order.scope, "work order scope")
    _text(order.authorization_ref, "work order authorization_ref")
    if order.product_project_id is not None:
        _text(order.product_project_id, "product_project_id")

    qa = snapshot.qa
    if qa is not None:
        if order.product_project_id is None or qa.project_id != order.product_project_id:
            raise BusinessFactoryError("QA record is not bound to ProductProject")
        _text(qa.evidence_ref, "QA evidence_ref")

    delivery = snapshot.delivery
    if delivery is not None:
        if qa is None or qa.state is not QAState.PASSED:
            raise BusinessFactoryError("delivery exists without passing QA")
        if delivery.project_id != order.product_project_id:
            raise BusinessFactoryError("delivery/ProductProject identity mismatch")
        _text(delivery.delivery_id, "delivery_id")
        _text(delivery.artifact_ref, "delivery artifact_ref")
        _text(delivery.authorization_ref, "delivery authorization_ref")
        _unique(
            delivery.compliance_evidence_refs,
            "delivery compliance evidence_ref",
            allow_empty=True,
        )

    payment = snapshot.payment
    if payment is not None:
        if delivery is None or payment.project_id != delivery.project_id:
            raise BusinessFactoryError("payment state exists without matching delivery")
        _text(payment.invoice_ref, "invoice_ref")
        _text(payment.evidence_ref, "payment evidence_ref")

    case_ids: set[str] = set()
    for case in snapshot.support_cases:
        if delivery is None or case.project_id != delivery.project_id:
            raise BusinessFactoryError("support case exists without matching delivery")
        if case.case_id in case_ids:
            raise BusinessFactoryError("duplicate support case_id")
        case_ids.add(case.case_id)
        _text(case.case_id, "support case_id")
        _text(case.summary, "support summary")
        _text(case.evidence_ref, "support evidence_ref")
        if case.state is SupportCaseState.RESOLVED and not case.resolution_ref:
            raise BusinessFactoryError("resolved support case requires resolution_ref")


def _opportunity(raw: object) -> MarketOpportunity | None:
    if raw is None:
        return None
    item = _mapping(raw, "opportunity")
    return MarketOpportunity(
        _value(item, "opportunity_id", "opportunity"),
        _value(item, "objective_id", "opportunity"),
        _value(item, "title", "opportunity"),
        _strings(item, "evidence_ids", "opportunity"),
    )


def _lead(raw: object) -> BusinessLead | None:
    if raw is None:
        return None
    item = _mapping(raw, "lead")
    return BusinessLead(
        _value(item, "lead_id", "lead"),
        _value(item, "opportunity_id", "lead"),
        _value(item, "channel_id", "lead"),
        _value(item, "counterparty_ref", "lead"),
        _optional_value(item, "qualification_ref", "lead"),
    )


def _proposal(raw: object) -> BusinessProposal | None:
    if raw is None:
        return None
    item = _mapping(raw, "proposal")
    return BusinessProposal(
        _value(item, "proposal_id", "proposal"),
        _value(item, "lead_id", "proposal"),
        _value(item, "scope_summary", "proposal"),
        _enum(ProposalState, item.get("state"), "proposal state"),
        _optional_value(item, "approval_ref", "proposal"),
    )


def _work_order(raw: object) -> BusinessWorkOrder | None:
    if raw is None:
        return None
    item = _mapping(raw, "work order")
    return BusinessWorkOrder(
        _value(item, "work_order_id", "work order"),
        _value(item, "proposal_id", "work order"),
        _value(item, "scope", "work order"),
        _value(item, "authorization_ref", "work order"),
        _optional_value(item, "product_project_id", "work order"),
    )


def _qa(raw: object) -> QARecord | None:
    if raw is None:
        return None
    item = _mapping(raw, "QA record")
    return QARecord(
        _value(item, "project_id", "QA record"),
        _enum(QAState, item.get("state"), "QA state"),
        _value(item, "evidence_ref", "QA record"),
    )


def _delivery(raw: object) -> DeliveryRecord | None:
    if raw is None:
        return None
    item = _mapping(raw, "delivery")
    return DeliveryRecord(
        _value(item, "delivery_id", "delivery"),
        _value(item, "project_id", "delivery"),
        _value(item, "artifact_ref", "delivery"),
        _value(item, "authorization_ref", "delivery"),
        _strings(item, "compliance_evidence_refs", "delivery"),
    )


def _payment(raw: object) -> PaymentRecord | None:
    if raw is None:
        return None
    item = _mapping(raw, "payment")
    return PaymentRecord(
        _value(item, "project_id", "payment"),
        _value(item, "invoice_ref", "payment"),
        _enum(InvoicePaymentState, item.get("state"), "payment state"),
        _value(item, "evidence_ref", "payment"),
    )


def _support(raw: object) -> SupportCase:
    item = _mapping(raw, "support case")
    return SupportCase(
        _value(item, "case_id", "support case"),
        _value(item, "project_id", "support case"),
        _value(item, "summary", "support case"),
        _value(item, "evidence_ref", "support case"),
        _enum(SupportCaseState, item.get("state"), "support state"),
        _optional_value(item, "resolution_ref", "support case"),
    )


def _audit(raw: object) -> BusinessAuditEvent:
    item = _mapping(raw, "audit event")
    return BusinessAuditEvent(
        _int(item.get("sequence"), "audit sequence"),
        _value(item, "event_type", "audit event"),
        _value(item, "subject_id", "audit event"),
        _value(item, "evidence_ref", "audit event"),
        _value(item, "recorded_at", "audit event"),
    )


def _mapping(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise BusinessFactoryError(f"{label} must be an object")
    return raw


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise BusinessFactoryError(f"{label} must be a list")
    return raw


def _items(raw: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise BusinessFactoryError(f"{label} {key} must be a list")
    return [_mapping(item, f"{label} {key}") for item in value]


def _value(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    _text(value, f"{label} {key}")
    return value


def _optional_value(raw: dict[str, Any], key: str, label: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    _text(value, f"{label} {key}")
    return value


def _strings(raw: dict[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise BusinessFactoryError(f"{label} {key} must be a list")
    result = tuple(value)
    _unique(result, f"{label} {key}", allow_empty=True)
    return result


def _enum(enum_type: type[StrEnum], raw: object, label: str) -> Any:
    if not isinstance(raw, str):
        raise BusinessFactoryError(f"{label} must be text")
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise BusinessFactoryError(f"{label} is invalid") from exc


def _int(raw: object, label: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise BusinessFactoryError(f"{label} must be an integer")
    return raw


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BusinessFactoryError(f"{label} must be non-empty text")


def _unique(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise BusinessFactoryError(f"{label}s must be a tuple")
    if not values and allow_empty:
        return
    for value in values:
        _text(value, label)
    if len(set(values)) != len(values):
        raise BusinessFactoryError(f"duplicate {label}")
