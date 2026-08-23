from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
)
from nika_core.product_factory_coordinator import (
    trusted_plan_fingerprint as compute_trusted_plan_fingerprint,
)
from nika_core.product_factory_orchestration import ProductRepositoryGraph
from nika_core.product_project import ProductProject

_LIVE_AUTHORITY_SCHEMA = "nika-product-factory-live-plan-authority-v1"
_LIVE_AUTHORITY_KEY = secrets.token_bytes(32)


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
    # Candidate-controlled bytes are never authority. These live-only fields are
    # deliberately excluded from __init__/serialization. The fingerprint is useful
    # diagnostic metadata; the keyed proof is what proves that the trusted host-side
    # ProductProject binding admitted this exact initial plan during this process.
    trusted_plan_fingerprint: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    trusted_plan_authority_proof: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )


def verify_live_checkpoint_authority(
    checkpoint: ProductProjectCoordinatorCheckpoint,
) -> str:
    """Verify the process-ephemeral host binding proof for a first checkpoint.

    This proof is intentionally not durable. After the first atomic checkpoint save,
    the host-task anchor is authoritative across process restart. A caller that merely
    knows or recomputes the plan fingerprint cannot mint this keyed host proof.
    """

    plan = checkpoint.coordinator.trusted_plan
    if plan is None:
        raise ProductProjectBindingError("checkpoint is missing immutable trusted plan")
    try:
        fingerprint = compute_trusted_plan_fingerprint(plan)
    except CoordinatorError as exc:
        raise ProductProjectBindingError("checkpoint trusted plan is invalid") from exc
    if checkpoint.trusted_plan_fingerprint != fingerprint:
        raise ProductProjectBindingError(
            "live checkpoint trusted-plan fingerprint does not match checkpoint plan"
        )
    proof = checkpoint.trusted_plan_authority_proof
    if proof is None:
        raise ProductProjectBindingError("checkpoint has no live host authority proof")
    expected = _sign_live_authority(
        project_id=checkpoint.project_id,
        spec_version=checkpoint.spec_version,
        row_version=checkpoint.row_version,
        fingerprint=fingerprint,
    )
    if not hmac.compare_digest(proof, expected):
        raise ProductProjectBindingError("checkpoint live host authority proof is invalid")
    return fingerprint


@dataclass(slots=True)
class ProductProjectCoordinatorBinding:
    """Thin PF1 -> PF2 compatibility boundary.

    PF1 remains the durable owner of ProductProject state. This adapter neither persists
    coordinator snapshots nor creates a second project store; the host persists the
    checkpoint wherever orchestration state is durably owned and must re-bind it against
    the current ProductProject before resume.

    A live checkpoint receives a process-ephemeral keyed proof for the initial trusted
    plan. The proof cannot be reconstructed from checkpoint bytes or from the public plan
    fingerprint alone. It only authorizes first-anchor establishment; restart authority
    subsequently comes from the independently persisted host-task anchor.
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
        fingerprint = coordinator.trusted_plan_fingerprint
        checkpoint = ProductProjectCoordinatorCheckpoint(
            project_id=self.project.project_id,
            spec_version=self.project.spec_version,
            row_version=self.project.row_version,
            coordinator=snapshot,
        )
        object.__setattr__(checkpoint, "trusted_plan_fingerprint", fingerprint)
        object.__setattr__(
            checkpoint,
            "trusted_plan_authority_proof",
            _sign_live_authority(
                project_id=checkpoint.project_id,
                spec_version=checkpoint.spec_version,
                row_version=checkpoint.row_version,
                fingerprint=fingerprint,
            ),
        )
        return checkpoint

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


def _sign_live_authority(
    *,
    project_id: str,
    spec_version: int,
    row_version: int,
    fingerprint: str,
) -> str:
    payload = json.dumps(
        (
            _LIVE_AUTHORITY_SCHEMA,
            project_id,
            spec_version,
            row_version,
            fingerprint,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_LIVE_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()
