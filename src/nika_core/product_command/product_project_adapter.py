from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
    ProductUserDecision,
)
from nika_core.product_decisions import ProductDecisionRepository, StoredProductDecision
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductProject,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
    ProductProjectStatusTransition,
)

_MAX_LABEL = 240
_MAX_DETAIL = 4000
_MAX_REFERENCE = 512


class ProductProjectDecisionUnavailableError(RuntimeError):
    """Legacy compatibility symbol retained for older callers."""


class ProductProjectPresentationConsistencyError(RuntimeError):
    """Raised when durable project state changes during one presentation read."""


class ProductProjectCommandService:
    """PF5 adapter over the integrated durable PF1 repositories and lifecycle."""

    def __init__(self, repository: ProductProjectRepository) -> None:
        self._repository = repository
        self._decisions = ProductDecisionRepository(repository.store)
        self._lifecycle = ProductProjectLifecycleService(repository.store)

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        spec: ProductProjectSpec,
        idempotency_key: str,
    ) -> ProductProjectDetail:
        self._repository.create(
            project_id=project_id,
            name=name,
            spec=spec,
            idempotency_key=idempotency_key,
        )
        return self.inspect_project(project_id)

    def inspect_project(self, project_id: str) -> ProductProjectDetail:
        detail, _credential_refs = self.inspect_project_context(project_id)
        return detail

    def inspect_project_context(
        self,
        project_id: str,
    ) -> tuple[ProductProjectDetail, tuple[str, ...]]:
        """Read one self-consistent project+decision snapshot using public PF1 APIs."""
        before = self._repository.get(project_id)
        decisions = self._decisions.list(project_id)
        after = self._repository.get(project_id)
        if (
            before.row_version != after.row_version
            or before.spec_version != after.spec_version
            or before.status != after.status
            or before.updated_at != after.updated_at
        ):
            raise ProductProjectPresentationConsistencyError(
                "ProductProject changed while PF5 was composing presentation; "
                "retry from a fresh snapshot"
            )
        return project_detail(after, decisions=decisions), after.spec.credential_refs

    def update_project(
        self,
        project_id: str,
        *,
        expected_spec_version: int,
        spec: ProductProjectSpec | None = None,
        goal: str | None = None,
        desired_outcome: str | None = None,
        hypothesis: str | None = None,
    ) -> ProductProjectDetail:
        current = self._repository.get(project_id)
        if current.spec_version != expected_spec_version:
            raise StaleProjectVersionError(
                f"stale ProductProject spec: expected {expected_spec_version}, "
                f"current {current.spec_version}"
            )
        if spec is not None and any(
            item is not None for item in (goal, desired_outcome, hypothesis)
        ):
            raise ValueError("spec replacement cannot be combined with partial fields")
        next_spec = spec or replace(
            current.spec,
            goal=current.spec.goal if goal is None else goal,
            desired_outcome=(
                current.spec.desired_outcome
                if desired_outcome is None
                else desired_outcome
            ),
            hypothesis=current.spec.hypothesis if hypothesis is None else hypothesis,
        )
        self._repository.update_spec(
            project_id,
            next_spec,
            expected_row_version=current.row_version,
        )
        return self.inspect_project(project_id)

    def record_decision(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
    ) -> ProductProjectDetail:
        self._decisions.record(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
        )
        return self.inspect_project(project_id)

    def persist_decision(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
    ) -> ProductProjectDetail:
        """Compatibility name for the now-real durable ProductDecision write path."""
        return self.record_decision(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
        )

    def link_decision_requirement(
        self,
        project_id: str,
        *,
        requirement_id: str,
        decision_id: str,
        expected_row_version: int,
    ) -> ProductProjectDetail:
        self._decisions.link_requirement(
            project_id,
            requirement_id=requirement_id,
            decision_id=decision_id,
            expected_row_version=expected_row_version,
        )
        return self.inspect_project(project_id)

    def decision_history(
        self,
        project_id: str,
        decision_id: str,
    ) -> tuple[ProductUserDecision, ...]:
        project = self._repository.get(project_id)
        return tuple(
            _decision_view(project, stored)
            for stored in self._decisions.history(project_id, decision_id)
        )

    def transition_project(
        self,
        project_id: str,
        new_state: ProductProjectState,
        *,
        expected_row_version: int,
        idempotency_key: str,
        reason: str,
        changed_by_ref: str,
    ) -> ProductProjectDetail:
        self._lifecycle.transition(
            project_id,
            new_state,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            reason=reason,
            changed_by_ref=changed_by_ref,
        )
        return self.inspect_project(project_id)

    def lifecycle_history(
        self,
        project_id: str,
    ) -> tuple[ProductProjectStatusTransition, ...]:
        return self._lifecycle.history(project_id)


def project_detail(
    project: ProductProject,
    *,
    decisions: tuple[StoredProductDecision, ...] = (),
) -> ProductProjectDetail:
    statuses = _spec_statuses(project)
    decision_views = tuple(_decision_view(project, item) for item in decisions)
    pending = tuple(item for item in decision_views if item.state == "pending")
    current_decision = pending[0] if len(pending) == 1 else None
    version_log = (
        f"Durable ProductProject spec version {project.spec_version}; "
        f"row={project.row_version}; state={project.status}."
    )
    if len(pending) > 1:
        version_log += (
            f" {len(pending)} product decisions require owner review; "
            "no single decision was auto-selected."
        )
    return ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id=_bounded_identity(project.project_id, 160),
            version=project.spec_version,
            title=_bounded(project.name, _MAX_LABEL),
            goal=_bounded(project.spec.goal, _MAX_DETAIL),
            state=_bounded(project.status, 80),
            updated_at=datetime.fromisoformat(project.updated_at),
            current_decision=current_decision,
            blocker_count=sum(
                item.kind is ProductStatusKind.BLOCKER for item in statuses
            ),
        ),
        statuses=statuses,
        decisions=decision_views,
        logs=(version_log,),
    )


def _decision_view(
    project: ProductProject,
    stored: StoredProductDecision,
) -> ProductUserDecision:
    decision = stored.decision
    state = {
        ProductDecisionState.PROPOSED: "pending",
        ProductDecisionState.APPROVED: "approved",
        ProductDecisionState.REJECTED: "rejected",
    }[decision.state]
    evidence = tuple(
        _evidence("research_package", package_id, "Decision research evidence")
        for package_id in stored.evidence_package_ids
    )
    rationale = decision.rationale.strip() or "No rationale recorded."
    return ProductUserDecision(
        decision_id=_bounded_identity(decision.decision_id, 160),
        title=_bounded(f"Product decision: {decision.option_id}", _MAX_LABEL),
        question=_bounded(
            f"Option {decision.option_id}. Rationale: {rationale}",
            _MAX_DETAIL,
        ),
        risk_level=_risk_level(project.spec.risk),
        state=state,
        evidence=evidence,
    )


def _spec_statuses(project: ProductProject) -> tuple[ProductStatusEntry, ...]:
    spec = project.spec
    entries: list[ProductStatusEntry] = []
    blocked_milestones = {
        milestone_id
        for blocker in spec.blockers
        for milestone_id in blocker.blocking_milestone_ids
    }
    for requirement in spec.requirements:
        evidence = tuple(
            _evidence("research_package", package_id, "Research evidence package")
            for package_id in requirement.evidence_package_ids
        )
        structured_acceptance = "; ".join(
            f"{criterion.criterion_id}: {criterion.text} "
            f"[{criterion.verification_method}]"
            for criterion in requirement.acceptance_criteria
        )
        detail_parts = [
            f"Kind: {requirement.kind.value}.",
            "Acceptance: " + "; ".join(requirement.acceptance),
        ]
        if structured_acceptance:
            detail_parts.append("Structured acceptance: " + structured_acceptance)
        if requirement.decision_ids:
            detail_parts.append(
                "Decision links: " + ", ".join(requirement.decision_ids)
            )
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.REQUIREMENT,
                item_id=_bounded_identity(requirement.requirement_id, 160),
                label=_bounded(requirement.text, _MAX_LABEL),
                state="defined",
                detail=_bounded(" ".join(detail_parts), _MAX_DETAIL),
                evidence=evidence,
            )
        )
    for milestone in spec.milestones:
        dependencies = ", ".join(milestone.depends_on_ids) or "none"
        criteria = ", ".join(milestone.acceptance_criterion_ids) or "none"
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.MILESTONE,
                item_id=_bounded_identity(milestone.milestone_id, 160),
                label=_bounded(milestone.title, _MAX_LABEL),
                state=(
                    "blocked"
                    if milestone.milestone_id in blocked_milestones
                    else "defined"
                ),
                detail=_bounded(
                    f"Dependencies: {dependencies}; acceptance criteria: {criteria}.",
                    _MAX_DETAIL,
                ),
            )
        )
    for blocker in spec.blockers:
        evidence = tuple(
            _evidence("blocker_evidence", ref, "Blocker evidence")
            for ref in blocker.evidence_refs
        )
        milestones = ", ".join(blocker.blocking_milestone_ids) or "project-wide"
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.BLOCKER,
                item_id=_bounded_identity(blocker.blocker_id, 160),
                label=_bounded(blocker.summary, _MAX_LABEL),
                state="active",
                detail=_bounded(f"Blocks: {milestones}.", _MAX_DETAIL),
                evidence=evidence,
            )
        )
    for architecture in spec.architecture_decisions:
        evidence = tuple(
            _evidence(
                "research_package",
                package_id,
                "Architecture decision evidence",
            )
            for package_id in architecture.evidence_package_ids
        )
        supersedes = (
            f" Supersedes: {architecture.supersedes_decision_id}."
            if architecture.supersedes_decision_id
            else ""
        )
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.ARCHITECTURE_DECISION,
                item_id=_bounded_identity(
                    architecture.architecture_decision_id,
                    160,
                ),
                label=_bounded(architecture.title, _MAX_LABEL),
                state=_bounded(architecture.status, 80),
                detail=_bounded(architecture.rationale + supersedes, _MAX_DETAIL),
                evidence=evidence,
            )
        )
    entries.extend(
        _reference_entries(
            ProductStatusKind.ARCHITECTURE_DECISION,
            "Architecture decision",
            spec.architecture_decision_refs,
        )
    )
    entries.extend(
        _reference_entries(
            ProductStatusKind.REPOSITORY,
            "Repository",
            spec.repository_refs,
        )
    )
    entries.extend(
        _reference_entries(ProductStatusKind.TEAM_ROLE, "Team", spec.team_refs)
    )
    entries.extend(_reference_entries(ProductStatusKind.BUILD, "Build", spec.build_refs))
    entries.extend(
        _reference_entries(ProductStatusKind.RELEASE, "Release", spec.release_refs)
    )
    entries.extend(
        _reference_entries(
            ProductStatusKind.DEPLOYMENT,
            "Deployment",
            spec.deployment_refs,
        )
    )
    entries.extend(
        _reference_entries(ProductStatusKind.INCIDENT, "Incident", spec.incident_refs)
    )
    return tuple(entries)


def _reference_entries(
    kind: ProductStatusKind,
    label_prefix: str,
    references: tuple[str, ...],
) -> list[ProductStatusEntry]:
    return [
        ProductStatusEntry(
            kind=kind,
            item_id=f"{kind.value}:{index}",
            label=_bounded(f"{label_prefix} {index}", _MAX_LABEL),
            state="referenced",
            detail=_bounded(reference, _MAX_DETAIL),
        )
        for index, reference in enumerate(references, start=1)
    ]


def _evidence(kind: str, reference: str, label: str) -> EvidenceReference:
    visible_reference = reference
    if len(visible_reference) > _MAX_REFERENCE:
        digest = hashlib.sha256(visible_reference.encode("utf-8")).hexdigest()
        visible_reference = f"sha256:{digest}"
    return EvidenceReference(
        kind=kind,
        reference=visible_reference,
        label=_bounded(label, _MAX_LABEL),
    )


def _risk_level(risk: dict) -> int:
    for key in ("risk_level", "level"):
        value = risk.get(key)
        if isinstance(value, int) and 0 <= value <= 4:
            return value
        if isinstance(value, str):
            normalized = value.strip().upper()
            if (
                len(normalized) == 2
                and normalized.startswith("R")
                and normalized[1].isdigit()
            ):
                parsed = int(normalized[1])
                if 0 <= parsed <= 4:
                    return parsed
    return 0


def _bounded_identity(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{value[: limit - len(digest) - 1]}~{digest}"


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"
