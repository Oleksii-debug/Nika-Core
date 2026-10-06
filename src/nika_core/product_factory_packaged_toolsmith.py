from __future__ import annotations

import re
from dataclasses import dataclass

from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryPreparationService,
    PreparedProductFactory,
)
from nika_core.product_factory_toolsmith_integration import (
    ComponentCapabilityGap,
    ComponentCapabilityResume,
    DurableProductFactoryRepairPort,
    ProductFactoryToolsmithBridge,
)

_GRAPH_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ATTEMPTED_METHODS = 16
_MAX_METHOD_TEXT_LENGTH = 256
_DURABLE_GAP_REASON = "Product Factory worker capability gap"


class PackagedProductFactoryToolsmithError(ValueError):
    """Trusted packaged Factory→Toolsmith handoff cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class PackagedProductFactoryCapabilityGapPlan:
    """Exact immutable identity for one trusted packaged capability-gap handoff."""

    project_id: str
    expected_spec_version: int
    expected_row_version: int
    expected_graph_digest: str
    component_id: str
    expected_work_id: str
    capability_id: str
    attempted_methods: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        project_id = _plain_text(self.project_id, "project_id")
        component_id = _plain_text(self.component_id, "component_id")
        expected_work_id = _plain_text(self.expected_work_id, "expected_work_id")
        capability_id = _plain_text(self.capability_id, "capability_id")
        if (
            type(self.expected_spec_version) is not int
            or self.expected_spec_version < 1
        ):
            raise PackagedProductFactoryToolsmithError(
                "expected_spec_version must be a positive integer"
            )
        if (
            type(self.expected_row_version) is not int
            or self.expected_row_version < 0
        ):
            raise PackagedProductFactoryToolsmithError(
                "expected_row_version must be a non-negative integer"
            )

        graph_digest = _plain_text(
            self.expected_graph_digest,
            "expected_graph_digest",
        ).casefold()
        if _GRAPH_DIGEST_RE.fullmatch(graph_digest) is None:
            raise PackagedProductFactoryToolsmithError(
                "expected_graph_digest must be a 64-character hexadecimal digest"
            )

        if type(self.attempted_methods) is not tuple:
            raise PackagedProductFactoryToolsmithError(
                "attempted_methods must be a tuple"
            )
        if len(self.attempted_methods) > _MAX_ATTEMPTED_METHODS:
            raise PackagedProductFactoryToolsmithError(
                "attempted_methods exceeds the bounded evidence limit"
            )
        attempted_methods = tuple(
            _bounded_method(item) for item in self.attempted_methods
        )
        if len(set(attempted_methods)) != len(attempted_methods):
            raise PackagedProductFactoryToolsmithError(
                "attempted_methods must not contain duplicates"
            )

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "expected_graph_digest", graph_digest)
        object.__setattr__(self, "component_id", component_id)
        object.__setattr__(self, "expected_work_id", expected_work_id)
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "attempted_methods", attempted_methods)


@dataclass(slots=True)
class _PackagedRepairAuthority(DurableProductFactoryRepairPort):
    preparation: PackagedProductFactoryPreparationService
    prepared: PreparedProductFactory

    def preview_repair(
        self,
        *,
        component_id: str,
        reason: str,
    ):
        return self.preparation.preview_repair(
            self.prepared,
            component_id=component_id,
            reason=reason,
        )

    def commit_repair(
        self,
        *,
        component_id: str,
        reason: str,
        expected_next_work_id: str,
    ):
        return self.preparation.commit_repair(
            self.prepared,
            component_id=component_id,
            reason=reason,
            expected_next_work_id=expected_next_work_id,
        )


@dataclass(slots=True)
class PackagedProductFactoryToolsmithService:
    """Presentation-neutral exact handoff from packaged Product Factory to Toolsmith.

    Capability identity is never inferred from command text or a worker-failure
    message. A trusted producer must retain the exact ProductProject/graph/work
    authority and nominate one explicit capability id. The incumbent durable
    bridge remains the sole Product Factory↔Toolsmith lifecycle authority.
    """

    preparation: PackagedProductFactoryPreparationService
    bridge: ProductFactoryToolsmithBridge

    def begin_gap(
        self,
        plan: PackagedProductFactoryCapabilityGapPlan,
    ) -> ComponentCapabilityGap:
        _require_plan(plan)
        prepared = self._restore_exact(plan)
        try:
            host_task_id, request = self.preparation.require_repair_request(
                plan.project_id,
                plan.component_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise PackagedProductFactoryToolsmithError(
                "current Product Factory repair authority is unavailable"
            ) from exc

        if host_task_id != prepared.host_task_id:
            raise PackagedProductFactoryToolsmithError(
                "Product Factory host task changed during capability-gap handoff"
            )
        if request.work_id != plan.expected_work_id:
            raise PackagedProductFactoryToolsmithError(
                "capability-gap plan is stale for the current component attempt"
            )

        try:
            return self.bridge.begin_durable_gap(
                request,
                host_task_id=host_task_id,
                capability_id=plan.capability_id,
                reason=_DURABLE_GAP_REASON,
                attempted_methods=plan.attempted_methods,
            )
        except Exception as exc:  # noqa: BLE001
            raise PackagedProductFactoryToolsmithError(
                "durable Toolsmith capability-gap handoff was rejected"
            ) from exc

    def resume_registered_gap(
        self,
        plan: PackagedProductFactoryCapabilityGapPlan,
    ) -> ComponentCapabilityResume | None:
        _require_plan(plan)
        prepared = self._restore_exact(plan)
        try:
            return self.bridge.resume_durable_registered_gap(
                host_task_id=prepared.host_task_id,
                binding=prepared.state.binding,
                coordinator=prepared.state.coordinator,
                component_id=plan.component_id,
                expected_work_id=plan.expected_work_id,
                expected_capability_id=plan.capability_id,
                repair_authority=_PackagedRepairAuthority(
                    self.preparation,
                    prepared,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise PackagedProductFactoryToolsmithError(
                "durable Toolsmith capability-gap resume was rejected"
            ) from exc

    def _restore_exact(
        self,
        plan: PackagedProductFactoryCapabilityGapPlan,
    ) -> PreparedProductFactory:
        try:
            prepared = self.preparation.restore(plan.project_id)
        except Exception as exc:  # noqa: BLE001
            raise PackagedProductFactoryToolsmithError(
                "current Product Factory authority is unavailable"
            ) from exc
        if (
            prepared.spec_version != plan.expected_spec_version
            or prepared.row_version != plan.expected_row_version
            or prepared.graph_digest != plan.expected_graph_digest
        ):
            raise PackagedProductFactoryToolsmithError(
                "capability-gap plan is stale for the current Product Factory authority"
            )
        return prepared


def _require_plan(plan: object) -> None:
    if type(plan) is not PackagedProductFactoryCapabilityGapPlan:
        raise TypeError(
            "plan must be PackagedProductFactoryCapabilityGapPlan"
        )


def _plain_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise PackagedProductFactoryToolsmithError(
            f"{label} must be normalized non-empty text without control characters"
        )
    return value


def _bounded_method(value: object) -> str:
    method = _plain_text(value, "attempted_methods item")
    if len(method.encode("utf-8")) > _MAX_METHOD_TEXT_LENGTH:
        raise PackagedProductFactoryToolsmithError(
            "attempted_methods item exceeds the bounded evidence limit"
        )
    return method
