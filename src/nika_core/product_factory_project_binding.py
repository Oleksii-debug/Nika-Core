from __future__ import annotations

from dataclasses import dataclass, field

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
)
from nika_core.product_factory_orchestration import ProductRepositoryGraph
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
    """Thin PF1 -> PF2 compatibility boundary.

    PF1 remains the durable owner of ProductProject state. This adapter neither persists
    coordinator snapshots nor creates a second project store; the host persists the
    checkpoint wherever orchestration state is durably owned and must re-bind it against
    the current ProductProject before resume.
    """

    project: ProductProject
    graph: ProductRepositoryGraph

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

    def plan(
        self,
        *,
        base_shas: dict[str, str],
        component_goals: dict[str, str],
        permission_ceiling: frozenset[str],
    ) -> ProductFactoryCoordinator:
        coordinator = ProductFactoryCoordinator(self.graph)
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
        coordinator = ProductFactoryCoordinator(self.graph)
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
