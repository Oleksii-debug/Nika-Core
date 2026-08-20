from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductProjectDetail,
    ProductProjectSummary,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_project import (
    ProductProject,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)


class ProductProjectDecisionUnavailableError(RuntimeError):
    """Raised when PF5 is asked to persist a decision without an integrated PF1 API."""


class ProductProjectCommandService:
    """PF5 command/presentation adapter over the integrated durable PF1 repository."""

    def __init__(self, repository: ProductProjectRepository) -> None:
        self._repository = repository

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        spec: ProductProjectSpec,
        idempotency_key: str,
    ) -> ProductProjectDetail:
        project = self._repository.create(
            project_id=project_id,
            name=name,
            spec=spec,
            idempotency_key=idempotency_key,
        )
        return project_detail(project)

    def inspect_project(self, project_id: str) -> ProductProjectDetail:
        return project_detail(self._repository.get(project_id))

    def project_credential_refs(self, project_id: str) -> tuple[str, ...]:
        """Return internal opaque credential references without projecting them to user detail."""
        return self._repository.get(project_id).spec.credential_refs

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
                current.spec.desired_outcome if desired_outcome is None else desired_outcome
            ),
            hypothesis=current.spec.hypothesis if hypothesis is None else hypothesis,
        )
        updated = self._repository.update_spec(
            project_id,
            next_spec,
            expected_row_version=current.row_version,
        )
        return project_detail(updated)

    def persist_decision(self, project_id: str, decision_id: str) -> None:
        self._repository.get(project_id)
        if not decision_id.strip():
            raise ValueError("decision_id must not be empty")
        raise ProductProjectDecisionUnavailableError(
            "integrated PF1 does not expose a durable decision-write API; "
            "PF5 will not bypass ProductProject ownership with direct SQL"
        )


def project_detail(project: ProductProject) -> ProductProjectDetail:
    statuses = _spec_statuses(project)
    version_log = (
        f"Durable ProductProject spec version {project.spec_version}; row={project.row_version}."
    )
    return ProductProjectDetail(
        summary=ProductProjectSummary(
            project_id=project.project_id,
            version=project.spec_version,
            title=project.name,
            goal=project.spec.goal,
            state=project.status,
            updated_at=datetime.fromisoformat(project.updated_at),
            blocker_count=sum(
                item.kind is ProductStatusKind.BLOCKER for item in statuses
            ),
        ),
        statuses=statuses,
        logs=(version_log,),
    )


def _spec_statuses(project: ProductProject) -> tuple[ProductStatusEntry, ...]:
    spec = project.spec
    entries: list[ProductStatusEntry] = []
    for requirement in spec.requirements:
        evidence = tuple(
            EvidenceReference(
                kind="research_package",
                reference=package_id,
                label="Research evidence package",
            )
            for package_id in requirement.evidence_package_ids
        )
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.REQUIREMENT,
                item_id=requirement.requirement_id,
                label=requirement.text,
                state="defined",
                detail="Acceptance: " + "; ".join(requirement.acceptance),
                evidence=evidence,
            )
        )
    entries.extend(
        _reference_entries(ProductStatusKind.REPOSITORY, "Repository", spec.repository_refs)
    )
    entries.extend(_reference_entries(ProductStatusKind.TEAM_ROLE, "Team", spec.team_refs))
    entries.extend(_reference_entries(ProductStatusKind.BUILD, "Build", spec.build_refs))
    entries.extend(_reference_entries(ProductStatusKind.RELEASE, "Release", spec.release_refs))
    entries.extend(
        _reference_entries(ProductStatusKind.DEPLOYMENT, "Deployment", spec.deployment_refs)
    )
    entries.extend(_reference_entries(ProductStatusKind.INCIDENT, "Incident", spec.incident_refs))
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
            label=f"{label_prefix} {index}",
            state="referenced",
            detail=reference,
        )
        for index, reference in enumerate(references, start=1)
    ]
