from __future__ import annotations

from dataclasses import dataclass

from nika_core.product_factory_build_deployment_handoff import (
    BuildDeploymentHandoff,
)
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionState,
)
from nika_core.product_factory_build_execution_host import DurableBuildExecutionHost
from nika_core.product_factory_deployment import DeploymentRecord
from nika_core.product_factory_multi_repository import MultiRepositoryExecutionState
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
)


class PackagedReviewedBuildLoopError(RuntimeError):
    """Raised when packaged PF4 -> PF5 -> PF6 orchestration cannot stay authoritative."""


@dataclass(slots=True)
class PackagedReviewedBuildLoopController:
    """Advance one independently reviewed Product Factory component through PF5.

    PF4 review/admission authority remains in PackagedBuildAuthorityRuntime and
    reviewed_component_build_spec. PF5 durability/effect ordering remains in
    DurableBuildExecutionHost. PF6 deployment remains an explicit, separately
    authorized operation through BuildDeploymentHandoff.

    This controller never derives argv from ProductRepositoryGraph and never replays
    an uncertain external build effect. reconcile_work is the only method here that
    asks the canonical PF5 node port to inspect such an effect.
    """

    authorities: PackagedBuildAuthorityRuntime
    build_host: DurableBuildExecutionHost
    deployment_handoff: BuildDeploymentHandoff | None = None

    def __post_init__(self) -> None:
        if type(self.authorities) is not PackagedBuildAuthorityRuntime:
            raise TypeError("authorities must be exact PackagedBuildAuthorityRuntime")
        if type(self.build_host) is not DurableBuildExecutionHost:
            raise TypeError("build_host must be exact DurableBuildExecutionHost")
        if self.deployment_handoff is not None:
            if type(self.deployment_handoff) is not BuildDeploymentHandoff:
                raise TypeError("deployment_handoff must be exact BuildDeploymentHandoff")
            if self.deployment_handoff.build_host is not self.build_host:
                raise PackagedReviewedBuildLoopError(
                    "PF6 handoff must share the exact packaged PF5 durable host"
                )

    def advance_component(
        self,
        *,
        state: MultiRepositoryExecutionState,
        component_id: str,
    ) -> BuildExecutionRecord:
        """Admit current accepted PF4 work and advance it at most through one PF5 effect.

        Normal retryable states may be prepared again under current host authority. A
        PREPARED/DISPATCHING work item may cross the external effect boundary once.
        EFFECT_IN_FLIGHT and RECONCILE_REQUIRED are returned unchanged; callers must use
        reconcile_work so an uncertain build is inspected rather than replayed.
        """

        if type(state) is not MultiRepositoryExecutionState:
            raise TypeError("state must be exact MultiRepositoryExecutionState")
        component_id = _canonical_text(component_id, "component_id")

        spec = self.authorities.admit_reviewed_component(
            authority=state.authority,
            coordinator=state.coordinator,
            component_id=component_id,
        )
        record = self.build_host.submit(spec)
        work_id = spec.request.work_id

        if record.state in {
            BuildExecutionState.PENDING,
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
        }:
            record = self.build_host.prepare(work_id)

        if record.state is BuildExecutionState.PREPARED:
            self.build_host.begin_dispatch(work_id)
            record = self.build_host.execute(work_id)
        elif record.state is BuildExecutionState.DISPATCHING:
            record = self.build_host.execute(work_id)

        return _require_exact_record(record, work_id=work_id)

    def reconcile_work(self, work_id: str) -> BuildExecutionRecord:
        """Explicitly inspect a PF5 effect whose outcome may be uncertain."""

        work_id = _canonical_text(work_id, "work_id")
        record = self._record(work_id)
        if record.state not in {
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
        }:
            return record
        reconciled = self.build_host.reconcile(work_id)
        return _require_exact_record(reconciled, work_id=work_id)

    def build_status(self, work_id: str) -> BuildExecutionRecord:
        """Return the exact durable PF5 record without causing an external effect."""

        return self._record(_canonical_text(work_id, "work_id"))

    def deploy_staging(self, work_id: str) -> DeploymentRecord:
        """Enter PF6 only through the injected canonical staging handoff authority."""

        work_id = _canonical_text(work_id, "work_id")
        if self.deployment_handoff is None:
            raise PackagedReviewedBuildLoopError(
                "packaged PF6 staging handoff is not configured"
            )
        return self.deployment_handoff.deploy_staging(work_id)

    def _record(self, work_id: str) -> BuildExecutionRecord:
        snapshot = self.build_host.snapshot().coordinator
        matches = tuple(
            record
            for record in snapshot.records
            if record.spec.request.work_id == work_id
        )
        if len(matches) != 1:
            raise PackagedReviewedBuildLoopError(
                "durable PF5 work identity is missing or ambiguous"
            )
        return _require_exact_record(matches[0], work_id=work_id)


def _require_exact_record(
    value: object,
    *,
    work_id: str,
) -> BuildExecutionRecord:
    if type(value) is not BuildExecutionRecord:
        raise PackagedReviewedBuildLoopError(
            "packaged PF5 host returned a noncanonical execution record"
        )
    if value.spec.request.work_id != work_id:
        raise PackagedReviewedBuildLoopError(
            "packaged PF5 host returned the wrong durable work identity"
        )
    return value


def _canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PackagedReviewedBuildLoopError(
            f"{label} must be exact normalized non-empty text"
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise PackagedReviewedBuildLoopError(f"{label} must be single-line text")
    return value
