from __future__ import annotations

from dataclasses import dataclass, field

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
)
from nika_core.product_factory_orchestration import ProductRepositoryGraph, TeamPlan
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityPort,
    TeamPlanReviewAuthority,
    team_plan_fingerprint_ref,
)
from nika_core.product_project import ProductProject


class ProductProjectBindingError(ValueError):
    """Raised when durable ProductProject identity cannot safely bind to PF2 state."""


class StaleProductProjectBindingError(ProductProjectBindingError):
    """Raised when orchestration state targets an obsolete ProductProject version."""


@dataclass(frozen=True, slots=True)
class ProductProjectCoordinatorCheckpoint:
    project_id: str
    spec_version: int
    row_version: int
    coordinator: CoordinatorSnapshot
    trusted_plan_fingerprint: str | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ProductProjectCoordinatorBinding:
    """Thin PF1 -> PF2 compatibility boundary with persisted PF2 review identity.

    PF1 remains the durable owner of ProductProject state. A project that persists one or
    more ``team_refs`` may only expose independent review through the exact persisted
    ``TeamPlan`` plus a host-owned evidence authority. The durable project must bind both
    the historical plan id and a semantic fingerprint of the exact TeamPlan content, so
    reusing one plan id with attacker-chosen role content cannot manufacture authority.
    Legacy projects without a persisted team assignment remain plan/recovery compatible,
    but they have no review authority and therefore fail closed if an ACCEPTED transition
    is attempted or restored.
    """

    project: ProductProject
    graph: ProductRepositoryGraph
    team_plan: TeamPlan | None = None
    review_evidence_authority: ProductFactoryReviewAuthorityPort | None = field(
        default=None,
        repr=False,
    )
    _review_authority: ProductFactoryReviewAuthorityPort | None = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.project.project_id != self.graph.project_id:
            raise ProductProjectBindingError(
                "ProductProject identity does not match repository graph project_id"
            )
        if self.project.status != "active":
            raise ProductProjectBindingError("ProductProject must be active for orchestration")
        declared = set(self.project.spec.repository_refs)
        graph_locators = {repository.locator for repository in self.graph.repositories}
        if graph_locators and not graph_locators <= declared:
            missing = sorted(graph_locators - declared)
            raise ProductProjectBindingError(
                f"repository graph contains locators not declared by ProductProject: {missing}"
            )
        self._bind_review_authority()

    @property
    def has_trusted_review_authority(self) -> bool:
        return self._review_authority is not None

    def plan(
        self,
        *,
        base_shas: dict[str, str],
        component_goals: dict[str, str],
        permission_ceiling: frozenset[str],
    ) -> ProductFactoryCoordinator:
        if self.team_plan is not None and permission_ceiling != self.team_plan.permission_ceiling:
            raise ProductProjectBindingError(
                "Product Factory permission ceiling disagrees with persisted TeamPlan"
            )
        coordinator = ProductFactoryCoordinator(
            self.graph,
            review_authority=self._review_authority,
        )
        coordinator.plan(
            base_shas=base_shas,
            goals=component_goals,
            permission_ceiling=permission_ceiling,
        )
        return coordinator

    def checkpoint(
        self,
        coordinator: ProductFactoryCoordinator,
    ) -> ProductProjectCoordinatorCheckpoint:
        snapshot = coordinator.snapshot()
        if snapshot.project_id != self.project.project_id:
            raise ProductProjectBindingError(
                "coordinator snapshot does not belong to bound ProductProject"
            )
        return ProductProjectCoordinatorCheckpoint(
            project_id=self.project.project_id,
            spec_version=self.project.spec_version,
            row_version=self.project.row_version,
            coordinator=snapshot,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )

    def restore(
        self,
        checkpoint: ProductProjectCoordinatorCheckpoint,
        *,
        trusted_plan_fingerprint: str | None = None,
    ) -> ProductFactoryCoordinator:
        self._validate_checkpoint(checkpoint)
        coordinator = ProductFactoryCoordinator(
            self.graph,
            review_authority=self._review_authority,
        )
        try:
            coordinator.restore(
                checkpoint.coordinator,
                trusted_plan_fingerprint=trusted_plan_fingerprint,
            )
        except CoordinatorError as exc:
            raise ProductProjectBindingError(
                "coordinator checkpoint failed trusted-plan validation"
            ) from exc
        return coordinator

    def _bind_review_authority(self) -> None:
        team_refs = self.project.spec.team_refs
        if not team_refs:
            if self.team_plan is not None or self.review_evidence_authority is not None:
                raise ProductProjectBindingError(
                    "trusted TeamPlan must be persisted in ProductProject team_refs before use"
                )
            self._review_authority = None
            return
        if self.team_plan is None or self.review_evidence_authority is None:
            raise ProductProjectBindingError(
                "persisted ProductProject team assignment requires TeamPlan and review evidence authority"
            )
        if self.team_plan.project_id != self.project.project_id:
            raise ProductProjectBindingError("TeamPlan project identity does not match ProductProject")
        if self.team_plan.plan_id not in team_refs:
            raise ProductProjectBindingError(
                "TeamPlan identity is not persisted by ProductProject team_refs"
            )
        expected_plan_ref = team_plan_fingerprint_ref(self.team_plan)
        if expected_plan_ref not in team_refs:
            raise ProductProjectBindingError(
                "TeamPlan content fingerprint is not persisted by ProductProject team_refs"
            )
        component_ids = {component.component_id for component in self.graph.components}
        assigned_ids = {
            component_id
            for role in self.team_plan.roles
            for component_id in role.component_ids
        }
        if not component_ids <= assigned_ids:
            missing = sorted(component_ids - assigned_ids)
            raise ProductProjectBindingError(
                f"TeamPlan does not cover repository graph components: {missing}"
            )
        independently_reviewed = {
            component_id
            for role in self.team_plan.roles
            if role.independent_review
            for component_id in role.component_ids
        }
        if not component_ids <= independently_reviewed:
            missing = sorted(component_ids - independently_reviewed)
            raise ProductProjectBindingError(
                f"TeamPlan has no independent reviewer for components: {missing}"
            )
        self._review_authority = TeamPlanReviewAuthority(
            self.team_plan,
            self.review_evidence_authority,
        )

    def _validate_checkpoint(
        self,
        checkpoint: ProductProjectCoordinatorCheckpoint,
    ) -> None:
        if checkpoint.project_id != self.project.project_id:
            raise ProductProjectBindingError(
                "checkpoint project_id does not match current ProductProject"
            )
        if checkpoint.coordinator.project_id != self.project.project_id:
            raise ProductProjectBindingError(
                "checkpoint coordinator identity does not match current ProductProject"
            )
        if (
            checkpoint.spec_version != self.project.spec_version
            or checkpoint.row_version != self.project.row_version
        ):
            raise StaleProductProjectBindingError(
                "ProductProject changed after orchestration checkpoint; explicit reconciliation required"
            )