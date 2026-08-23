from __future__ import annotations

from nika_core.workspace_sdk.contracts import (
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
    WorkspaceValidationCatalog,
    compile_workspace_activation,
)
from nika_core.workspace_sdk.repository import WorkspacePluginRepository


class WorkspaceActivationService:
    """Recompiles live compatibility and approvals immediately before workspace activation."""

    def __init__(
        self,
        repository: WorkspacePluginRepository,
        catalog: WorkspaceValidationCatalog,
    ) -> None:
        self._repository = repository
        self._catalog = catalog

    def activate(
        self,
        manifest: WorkspaceManifest,
        entrypoint: WorkspaceEntrypointDescriptor,
        *,
        approved_permission_ids: frozenset[str],
    ) -> None:
        proposal = compile_workspace_activation(
            manifest,
            entrypoint,
            catalog=self._catalog,
            approved_permission_ids=approved_permission_ids,
        )
        self._repository.activate(proposal)
