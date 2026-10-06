from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.contracts import ProductProjectDetail
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_decisions import ProductDecisionSetSummary
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
)
from nika_core.product_factory_coordinator import CoordinatorSnapshot
from nika_core.product_factory_packaged_preparation import (
    PRODUCT_FACTORY_HOST_AGENT_ID,
    product_factory_host_task_identity,
)
from nika_core.product_project import ProductProjectRepository

PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID = "packaged.product-factory"
_PRODUCT_FACTORY_HOST_KIND = "product_factory"


class PackagedProductFactoryStatusError(ValueError):
    """Durable Product Factory status cannot be projected safely."""


class PackagedProductFactoryStatusReader:
    """Read the canonical current-version Product Factory coordinator checkpoint.

    This adapter is presentation-only. It never creates a host task, graph, checkpoint,
    worker dispatch, review, repair generation, or Toolsmith escalation. Absence of the
    deterministic current-version host task means the ProductProject is not prepared yet.
    Once that host exists, missing/corrupt/conflicting durable authority fails closed.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        workspace_id: str = PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
    ) -> None:
        if (
            type(workspace_id) is not str
            or not workspace_id.strip()
            or workspace_id != workspace_id.strip()
        ):
            raise PackagedProductFactoryStatusError(
                "workspace_id must be normalized non-empty text"
            )
        self._projects = ProductProjectRepository(store)
        self._tasks = TaskQueue(store)
        self._checkpoints = ProductFactoryCheckpointHost(store)
        self._workspace_id = workspace_id

    def read(self, project_id: str) -> CoordinatorSnapshot | None:
        before = self._projects.get(project_id)
        host_task_id = product_factory_host_task_identity(
            before.project_id,
            spec_version=before.spec_version,
            row_version=before.row_version,
        )
        try:
            task = self._tasks.get(host_task_id)
        except KeyError:
            return None
        except TaskPayloadCorruptionError as exc:
            raise PackagedProductFactoryStatusError(
                "Product Factory host task payload is corrupt"
            ) from exc

        if (
            task.workspace_id != self._workspace_id
            or task.agent_id != PRODUCT_FACTORY_HOST_AGENT_ID
            or task.payload.get("kind") != _PRODUCT_FACTORY_HOST_KIND
            or task.payload.get("product_project_id") != before.project_id
        ):
            raise PackagedProductFactoryStatusError(
                "deterministic Product Factory host task conflicts with packaged authority"
            )

        try:
            persisted = self._checkpoints.latest(
                host_task_id=host_task_id,
                project_id=before.project_id,
            )
        except ProductFactoryCheckpointError as exc:
            raise PackagedProductFactoryStatusError(
                "durable Product Factory checkpoint authority is invalid"
            ) from exc
        if persisted is None:
            raise PackagedProductFactoryStatusError(
                "Product Factory host task exists without a canonical coordinator checkpoint"
            )

        after = self._projects.get(project_id)
        if before != after:
            raise PackagedProductFactoryStatusError(
                "ProductProject changed while Product Factory status was read"
            )

        checkpoint = persisted.checkpoint
        if (
            checkpoint.project_id != after.project_id
            or checkpoint.spec_version != after.spec_version
            or checkpoint.row_version != after.row_version
            or checkpoint.coordinator.project_id != after.project_id
        ):
            raise PackagedProductFactoryStatusError(
                "durable Product Factory checkpoint is stale for the current ProductProject"
            )
        return checkpoint.coordinator


class PackagedProductCommandCenter:
    """Thin packaged PF5 composition that adds trusted Product Factory status."""

    def __init__(
        self,
        *,
        products: ProductProjectCommandService,
        status_reader: PackagedProductFactoryStatusReader,
    ) -> None:
        self._base = ProductCommandCenter(products)
        self._status_reader = status_reader

    def inspect_project(self, project_id: str) -> ProductProjectDetail:
        try:
            coordinator = self._status_reader.read(project_id)
            detail = self._base.inspect_project(project_id, coordinator=coordinator)
            confirmed = self._status_reader.read(project_id)
        except PackagedProductFactoryStatusError as exc:
            raise ProductProjectPresentationConsistencyError(
                "durable Product Factory status failed trusted projection"
            ) from exc
        if coordinator != confirmed:
            raise ProductProjectPresentationConsistencyError(
                "Product Factory status changed while PF5 was composing presentation; retry"
            )
        return detail

    def inspect_packaged_project(
        self,
        project_id: str,
    ) -> tuple[ProductProjectDetail, ProductDecisionSetSummary]:
        """Compose durable Factory status without materializing the complete decision set."""

        try:
            coordinator = self._status_reader.read(project_id)
            detail, decision_summary = self._base.inspect_packaged_project(
                project_id,
                coordinator=coordinator,
            )
            confirmed = self._status_reader.read(project_id)
        except PackagedProductFactoryStatusError as exc:
            raise ProductProjectPresentationConsistencyError(
                "durable Product Factory status failed trusted projection"
            ) from exc
        if coordinator != confirmed:
            raise ProductProjectPresentationConsistencyError(
                "Product Factory status changed while PF5 was composing presentation; retry"
            )
        return detail, decision_summary
