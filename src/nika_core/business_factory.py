from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.business_authority import (
    BusinessAuthorizationAuthorityPort,
    BusinessAuthorizationIntent,
    trusted_business_authorization,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductProject,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)

BUSINESS_FACTORY_SCHEMA = "nika.business_factory.pf9.v1"

_PF9_LINEAGE_KEYS = frozenset(
    {
        "business_work_order_authorization_ref",
        "business_work_order_authorization_fingerprint",
        "business_product_spec_fingerprint",
        "business_objective_ref",
        "business_handoff_effect_key",
    }
)
_TOKEN_VALUE = re.compile(
    r"(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
            raise BusinessFactoryError(
                "standing_policy_ref requires standing-policy communication authority"
            )


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
    approval_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class BusinessWorkOrder:
    work_order_id: str
    proposal_id: str
    scope: str
    target_project_id: str
    target_project_name: str
    product_spec_fingerprint: str
    authorization_ref: str
    authorization_fingerprint: str
    product_project_id: str | None = None


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
    audit: tuple[BusinessAuditEvent, ...] = ()
    row_version: int = 0


class BusinessFactory:
    """PF9 business-intake to ProductProject coordinator.

    This aggregate deliberately has no external-send, contract-signing, account-management,
    payment, deployment, or release-compliance executor. Material proposal and WorkOrder
    authorization is delegated to a trusted host authority and fails closed by default.
    """

    def __init__(
        self,
        snapshot: BusinessFactorySnapshot,
        *,
        approval_authority: BusinessAuthorizationAuthorityPort | None = None,
    ) -> None:
        _validate_snapshot(snapshot)
        self._snapshot = snapshot
        self._approval_authority = approval_authority

    @classmethod
    def start(
        cls,
        *,
        objective: BusinessObjective,
        policy: BusinessPolicy,
        approval_authority: BusinessAuthorizationAuthorityPort | None = None,
    ) -> BusinessFactory:
        snapshot = BusinessFactorySnapshot(
            schema=BUSINESS_FACTORY_SCHEMA,
            objective=objective,
            policy=policy,
        )
        factory = cls(snapshot, approval_authority=approval_authority)
        factory._record(
            "objective.created",
            objective.objective_id,
            objective.research_package.package_id,
        )
        return factory

    @classmethod
    def restore(
        cls,
        snapshot: BusinessFactorySnapshot,
        *,
        approval_authority: BusinessAuthorizationAuthorityPort | None = None,
    ) -> BusinessFactory:
        return cls(snapshot, approval_authority=approval_authority)

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
        intent = _proposal_authorization_intent(
            self._snapshot.objective.objective_id,
            proposal,
        )
        self._require_authorization(intent, approval_ref)
        proposal = replace(
            proposal,
            state=ProposalState.APPROVED,
            approval_ref=approval_ref,
            approval_fingerprint=intent.fingerprint,
        )
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
        target_project_id: str,
        target_project_name: str,
        product_spec: ProductProjectSpec,
        authorization_ref: str,
    ) -> BusinessWorkOrder:
        proposal = self._require_proposal()
        if proposal.state is not ProposalState.APPROVED:
            raise BusinessFactoryError("work order requires an approved proposal")
        if self._snapshot.work_order is not None:
            raise BusinessFactoryError("work order already exists")
        _text(work_order_id, "work_order_id")
        _text(scope, "work order scope")
        _text(target_project_id, "target ProductProject project_id")
        _text(target_project_name, "target ProductProject name")
        _text(authorization_ref, "work order authorization_ref")
        if not isinstance(product_spec, ProductProjectSpec):
            raise BusinessFactoryError("work order product_spec must be ProductProjectSpec")
        _validate_authorized_product_spec(product_spec, work_order_id=work_order_id)
        product_spec_fingerprint = _product_spec_fingerprint(product_spec)
        intent = _work_order_authorization_intent(
            self._snapshot.objective.objective_id,
            proposal,
            work_order_id=work_order_id,
            scope=scope,
            target_project_id=target_project_id,
            target_project_name=target_project_name,
            product_spec_fingerprint=product_spec_fingerprint,
        )
        self._require_authorization(intent, authorization_ref)
        order = BusinessWorkOrder(
            work_order_id=work_order_id,
            proposal_id=proposal.proposal_id,
            scope=scope,
            target_project_id=target_project_id,
            target_project_name=target_project_name,
            product_spec_fingerprint=product_spec_fingerprint,
            authorization_ref=authorization_ref,
            authorization_fingerprint=intent.fingerprint,
        )
        self._snapshot = replace(self._snapshot, work_order=order)
        self._record("work_order.authorized", work_order_id, authorization_ref)
        return order

    def handoff_to_product_factory(
        self,
        *,
        repository: ProductProjectRepository,
        spec: ProductProjectSpec,
        idempotency_key: str,
    ) -> BusinessWorkOrder:
        order = self._require_work_order()
        _text(idempotency_key, "ProductProject handoff request key")
        if not isinstance(spec, ProductProjectSpec):
            raise BusinessFactoryError("ProductProject handoff requires ProductProjectSpec")
        _validate_authorized_product_spec(spec, work_order_id=order.work_order_id)
        if _product_spec_fingerprint(spec) != order.product_spec_fingerprint:
            raise BusinessFactoryError(
                "ProductProject spec does not match the authorized WorkOrder specification"
            )

        objective_id = self._snapshot.objective.objective_id
        operation_key = _handoff_effect_key(objective_id, order.work_order_id)
        compliance = dict(spec.compliance)
        compliance.update(
            {
                "business_work_order_authorization_ref": order.authorization_ref,
                "business_work_order_authorization_fingerprint": order.authorization_fingerprint,
                "business_product_spec_fingerprint": order.product_spec_fingerprint,
                "business_objective_ref": objective_id,
                "business_handoff_effect_key": operation_key,
            }
        )
        bound_spec = replace(spec, compliance=compliance)
        try:
            project = repository.create(
                project_id=order.target_project_id,
                name=order.target_project_name,
                spec=bound_spec,
                idempotency_key=operation_key,
            )
        except (KeyError, ProductProjectError) as exc:
            raise BusinessFactoryError(
                "ProductProject handoff conflicts with durable WorkOrder effect"
            ) from exc

        _validate_product_effect(
            project,
            order=order,
            objective_id=objective_id,
            operation_key=operation_key,
        )
        if order.product_project_id is not None:
            if order.product_project_id != project.project_id:
                raise BusinessFactoryError(
                    "work order is already linked to a different ProductProject"
                )
            return order

        linked = replace(order, product_project_id=project.project_id)
        self._snapshot = replace(self._snapshot, work_order=linked)
        request_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        self._record(
            "product_project.linked",
            project.project_id,
            f"request-sha256:{request_digest}",
        )
        return linked

    def _require_authorization(
        self,
        intent: BusinessAuthorizationIntent,
        evidence_ref: str,
    ) -> None:
        if not trusted_business_authorization(
            self._approval_authority,
            intent=intent,
            evidence_ref=evidence_ref,
        ):
            raise BusinessFactoryError(
                "trusted business approval authority rejected or could not verify the action"
            )

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
        "audit",
        "row_version",
    }
    if set(raw) != expected:
        raise BusinessFactoryError("business snapshot fields do not match schema")

    objective_raw = _mapping(raw["objective"], "objective")
    package_raw = _mapping(objective_raw.get("research_package"), "research package")
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
        audit=tuple(_audit(item) for item in _list(raw["audit"], "audit")),
        row_version=_int(raw["row_version"], "row_version"),
    )
    _validate_snapshot(snapshot)
    return snapshot


def _validate_snapshot(snapshot: BusinessFactorySnapshot) -> None:
    if not isinstance(snapshot, BusinessFactorySnapshot):
        raise BusinessFactoryError("business snapshot type is invalid")
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
        if any(
            item is not None
            for item in (snapshot.lead, snapshot.proposal, snapshot.work_order)
        ):
            raise BusinessFactoryError("downstream business state exists without opportunity")
        return
    if opportunity.objective_id != snapshot.objective.objective_id:
        raise BusinessFactoryError("opportunity/objective identity mismatch")
    known_evidence = {item.evidence_id for item in snapshot.objective.research_package.evidence}
    if not set(opportunity.evidence_ids).issubset(known_evidence):
        raise BusinessFactoryError("opportunity contains unknown research evidence")

    lead = snapshot.lead
    if lead is None:
        if snapshot.proposal is not None or snapshot.work_order is not None:
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
        if snapshot.work_order is not None:
            raise BusinessFactoryError("downstream business state exists without proposal")
        return
    _text(proposal.proposal_id, "proposal_id")
    _text(proposal.scope_summary, "proposal scope_summary")
    if proposal.lead_id != lead.lead_id or not lead.qualified:
        raise BusinessFactoryError("proposal requires the qualified restored lead")
    if not isinstance(proposal.state, ProposalState):
        raise BusinessFactoryError("proposal state is invalid")
    if proposal.state is ProposalState.APPROVED:
        if not proposal.approval_ref or not proposal.approval_fingerprint:
            raise BusinessFactoryError("approved proposal is missing trusted approval evidence")
        _digest(proposal.approval_fingerprint, "proposal approval fingerprint")
        expected = _proposal_authorization_intent(
            snapshot.objective.objective_id,
            proposal,
        ).fingerprint
        if proposal.approval_fingerprint != expected:
            raise BusinessFactoryError(
                "proposal approval fingerprint does not match approved scope"
            )
    elif proposal.approval_fingerprint is not None:
        raise BusinessFactoryError("non-approved proposal cannot carry approval fingerprint")
    if proposal.approval_ref is not None:
        _text(proposal.approval_ref, "proposal approval_ref")

    order = snapshot.work_order
    if order is None:
        return
    if proposal.state is not ProposalState.APPROVED or order.proposal_id != proposal.proposal_id:
        raise BusinessFactoryError("work order requires the approved restored proposal")
    _text(order.work_order_id, "work_order_id")
    _text(order.scope, "work order scope")
    _text(order.target_project_id, "target ProductProject project_id")
    _text(order.target_project_name, "target ProductProject name")
    _digest(order.product_spec_fingerprint, "work order product spec fingerprint")
    _text(order.authorization_ref, "work order authorization_ref")
    _digest(order.authorization_fingerprint, "work order authorization fingerprint")
    expected_order_fingerprint = _work_order_authorization_intent(
        snapshot.objective.objective_id,
        proposal,
        work_order_id=order.work_order_id,
        scope=order.scope,
        target_project_id=order.target_project_id,
        target_project_name=order.target_project_name,
        product_spec_fingerprint=order.product_spec_fingerprint,
    ).fingerprint
    if order.authorization_fingerprint != expected_order_fingerprint:
        raise BusinessFactoryError("work order authorization fingerprint does not match effect")
    if order.product_project_id is not None:
        _text(order.product_project_id, "product_project_id")
        if order.product_project_id != order.target_project_id:
            raise BusinessFactoryError(
                "linked ProductProject identity differs from authorized target"
            )


def _proposal_authorization_intent(
    objective_id: str,
    proposal: BusinessProposal,
) -> BusinessAuthorizationIntent:
    return BusinessAuthorizationIntent(
        objective_id=objective_id,
        purpose="proposal.approve",
        subject_id=proposal.proposal_id,
        bindings=(
            ("lead_id", proposal.lead_id),
            ("scope_summary", proposal.scope_summary),
        ),
    )


def _work_order_authorization_intent(
    objective_id: str,
    proposal: BusinessProposal,
    *,
    work_order_id: str,
    scope: str,
    target_project_id: str,
    target_project_name: str,
    product_spec_fingerprint: str,
) -> BusinessAuthorizationIntent:
    if proposal.approval_fingerprint is None:
        raise BusinessFactoryError("approved proposal is missing trusted approval fingerprint")
    _digest(proposal.approval_fingerprint, "proposal approval fingerprint")
    _digest(product_spec_fingerprint, "product spec fingerprint")
    return BusinessAuthorizationIntent(
        objective_id=objective_id,
        purpose="work_order.authorize",
        subject_id=work_order_id,
        bindings=(
            ("product_spec_fingerprint", product_spec_fingerprint),
            ("proposal_approval_fingerprint", proposal.approval_fingerprint),
            ("proposal_id", proposal.proposal_id),
            ("scope", scope),
            ("target_project_id", target_project_id),
            ("target_project_name", target_project_name),
        ),
    )


def _product_spec_fingerprint(spec: ProductProjectSpec) -> str:
    effective_spec = replace(
        spec,
        supersedes_spec_version=None,
        revision_reason="initial specification",
    )
    payload = json.dumps(
        effective_spec.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_authorized_product_spec(spec: ProductProjectSpec, *, work_order_id: str) -> None:
    if spec.compliance.get("business_work_order_ref") != work_order_id:
        raise BusinessFactoryError(
            "ProductProject spec must bind the exact authorized business WorkOrder"
        )
    injected = _PF9_LINEAGE_KEYS.intersection(spec.compliance)
    if injected:
        raise BusinessFactoryError(
            "caller ProductProject spec cannot pre-populate trusted PF9 lineage fields: "
            + ", ".join(sorted(injected))
        )


def _handoff_effect_key(objective_id: str, work_order_id: str) -> str:
    payload = json.dumps(
        {"objective_id": objective_id, "work_order_id": work_order_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"nika-pf9-handoff-v2:{digest}"


def _validate_product_effect(
    project: ProductProject,
    *,
    order: BusinessWorkOrder,
    objective_id: str,
    operation_key: str,
) -> None:
    if project.project_id != order.target_project_id or project.name != order.target_project_name:
        raise BusinessFactoryError(
            "durable ProductProject identity does not match WorkOrder target"
        )
    compliance = project.spec.compliance
    expected = {
        "business_work_order_ref": order.work_order_id,
        "business_work_order_authorization_ref": order.authorization_ref,
        "business_work_order_authorization_fingerprint": order.authorization_fingerprint,
        "business_product_spec_fingerprint": order.product_spec_fingerprint,
        "business_objective_ref": objective_id,
        "business_handoff_effect_key": operation_key,
    }
    for key, value in expected.items():
        actual = compliance.get(key)
        if project.spec_version == 1:
            if actual != value:
                raise BusinessFactoryError(
                    f"durable ProductProject PF9 lineage mismatch at {key}"
                )
        elif actual is not None and actual != value:
            raise BusinessFactoryError(
                f"current ProductProject PF9 lineage conflicts at {key}"
            )


def _opportunity(raw: object) -> MarketOpportunity | None:
    if raw is None:
        return None
    item = _mapping(raw, "opportunity")
    return MarketOpportunity(
        opportunity_id=_value(item, "opportunity_id", "opportunity"),
        objective_id=_value(item, "objective_id", "opportunity"),
        title=_value(item, "title", "opportunity"),
        evidence_ids=_strings(item, "evidence_ids", "opportunity"),
    )


def _lead(raw: object) -> BusinessLead | None:
    if raw is None:
        return None
    item = _mapping(raw, "lead")
    return BusinessLead(
        lead_id=_value(item, "lead_id", "lead"),
        opportunity_id=_value(item, "opportunity_id", "lead"),
        channel_id=_value(item, "channel_id", "lead"),
        counterparty_ref=_value(item, "counterparty_ref", "lead"),
        qualification_ref=_optional_value(item, "qualification_ref", "lead"),
    )


def _proposal(raw: object) -> BusinessProposal | None:
    if raw is None:
        return None
    item = _mapping(raw, "proposal")
    return BusinessProposal(
        proposal_id=_value(item, "proposal_id", "proposal"),
        lead_id=_value(item, "lead_id", "proposal"),
        scope_summary=_value(item, "scope_summary", "proposal"),
        state=_enum(ProposalState, item.get("state"), "proposal state"),
        approval_ref=_optional_value(item, "approval_ref", "proposal"),
        approval_fingerprint=_optional_value(item, "approval_fingerprint", "proposal"),
    )


def _work_order(raw: object) -> BusinessWorkOrder | None:
    if raw is None:
        return None
    item = _mapping(raw, "work order")
    return BusinessWorkOrder(
        work_order_id=_value(item, "work_order_id", "work order"),
        proposal_id=_value(item, "proposal_id", "work order"),
        scope=_value(item, "scope", "work order"),
        target_project_id=_value(item, "target_project_id", "work order"),
        target_project_name=_value(item, "target_project_name", "work order"),
        product_spec_fingerprint=_value(item, "product_spec_fingerprint", "work order"),
        authorization_ref=_value(item, "authorization_ref", "work order"),
        authorization_fingerprint=_value(
            item,
            "authorization_fingerprint",
            "work order",
        ),
        product_project_id=_optional_value(item, "product_project_id", "work order"),
    )


def _audit(raw: object) -> BusinessAuditEvent:
    item = _mapping(raw, "audit event")
    return BusinessAuditEvent(
        sequence=_int(item.get("sequence"), "audit sequence"),
        event_type=_value(item, "event_type", "audit event"),
        subject_id=_value(item, "subject_id", "audit event"),
        evidence_ref=_value(item, "evidence_ref", "audit event"),
        recorded_at=_value(item, "recorded_at", "audit event"),
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


def _digest(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BusinessFactoryError(f"{label} must be a lowercase SHA-256 digest")


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BusinessFactoryError(f"{label} must be non-empty text")
    if _TOKEN_VALUE.search(value):
        raise BusinessFactoryError(
            f"raw credential material is forbidden at {label}; store an opaque reference"
        )


def _unique(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise BusinessFactoryError(f"{label}s must be a tuple")
    if not values and allow_empty:
        return
    for value in values:
        _text(value, label)
    if len(set(values)) != len(values):
        raise BusinessFactoryError(f"duplicate {label}")
