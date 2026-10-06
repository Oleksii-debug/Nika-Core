from __future__ import annotations

from dataclasses import dataclass

from nika_core.product_factory_build_deployment_handoff import BuildDeploymentHandoff
from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_deployment import DeploymentRecord
from nika_core.product_factory_deployment_checkpoint import DurableDeploymentFabric
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_build_pass import (
    PackagedComponentBuildState,
    PackagedReviewedBuildPass,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.product_factory_packaged_staging_authority import (
    PackagedStagingAuthorityError,
    PackagedStagingAuthorityStore,
)


class PackagedStagingPassError(RuntimeError):
    """Raised when one reviewed packaged build cannot safely enter PF6 staging."""


@dataclass(frozen=True, slots=True)
class PackagedComponentStagingResult:
    build: PackagedComponentBuildState
    deployment: DeploymentRecord | None


@dataclass(slots=True)
class PackagedReviewedStagingPass:
    """Advance one explicit reviewed component through PF5 and authorized PF6 staging.

    The build pass remains the PF4/PF5 composition authority. This layer does not
    select release versions, provider targets, inventories or credentials. A finished
    PF5 work item must already have exact durable PackagedStagingAuthorityStore
    authorization before canonical BuildDeploymentHandoff may reach the provider.

    The explicit component boundary is deliberate: PF6 currently models one exact
    ReleaseRef per environment, not a multi-component atomic release transaction.
    """

    build_pass: PackagedReviewedBuildPass
    deployment: DurableDeploymentFabric
    staging_authority: PackagedStagingAuthorityStore

    def __post_init__(self) -> None:
        if type(self.build_pass) is not PackagedReviewedBuildPass:
            raise TypeError("staging pass requires exact PackagedReviewedBuildPass")
        if type(self.deployment) is not DurableDeploymentFabric:
            raise TypeError("staging pass requires exact DurableDeploymentFabric")
        if type(self.staging_authority) is not PackagedStagingAuthorityStore:
            raise TypeError(
                "staging pass requires exact PackagedStagingAuthorityStore"
            )

    def advance_component(
        self,
        prepared: PreparedProductFactory,
        *,
        component_id: str,
    ) -> PackagedComponentStagingResult:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be exact PreparedProductFactory")
        if (
            type(component_id) is not str
            or not component_id
            or component_id != component_id.strip()
        ):
            raise PackagedStagingPassError(
                "component_id must be canonical non-empty text"
            )

        build_result = self.build_pass.advance(prepared)
        if build_result.project_id != prepared.project_id:
            raise PackagedStagingPassError(
                "packaged build pass returned another ProductProject"
            )
        matches = tuple(
            item for item in build_result.components if item.component_id == component_id
        )
        if len(matches) != 1:
            raise PackagedStagingPassError(
                "requested component is not one exact accepted packaged build"
            )
        selected = matches[0]
        if selected.state is not BuildExecutionState.SUCCEEDED:
            return PackagedComponentStagingResult(selected, None)

        host = build_packaged_local_durable_build_host(
            self.build_pass.store,
            host_task_id=prepared.host_task_id,
            project_id=prepared.project_id,
            node=self.build_pass.node,
            startup=self.build_pass.startup,
            trusted_authority=self.build_pass.authority.trusted_execution,
            output_policies=self.build_pass.authority.output_policies,
        )
        durable = host.coordinator.get(selected.work_id)
        if (
            durable.state is not BuildExecutionState.SUCCEEDED
            or durable.spec.request.project_id != prepared.project_id
            or durable.spec.request.work_id != selected.work_id
        ):
            raise PackagedStagingPassError(
                "PF5 durable build state changed before staging admission"
            )

        try:
            self.staging_authority.resolve(
                project_id=prepared.project_id,
                repository_id=durable.spec.scope.repository_id,
                work_id=selected.work_id,
            )
        except PackagedStagingAuthorityError as exc:
            raise PackagedStagingPassError(
                "finished PF5 build lacks current packaged staging authorization"
            ) from exc

        handoff = BuildDeploymentHandoff(
            build_host=host,
            deployment=self.deployment,
            authority=self.staging_authority,
        )
        deployment = handoff.deploy_staging(selected.work_id)
        return PackagedComponentStagingResult(selected, deployment)
