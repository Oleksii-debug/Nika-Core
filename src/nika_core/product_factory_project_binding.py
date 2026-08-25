from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from typing import Any
from urllib.parse import unquote, urlsplit

from nika_core.product_command.reference_safety import safe_evidence_reference
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    WorkRecord,
)
from nika_core.product_factory_coordinator import (
    trusted_plan_fingerprint as compute_trusted_plan_fingerprint,
)
from nika_core.product_factory_orchestration import ProductRepositoryGraph
from nika_core.product_project import ProductProject

_LIVE_AUTHORITY_SCHEMA = "nika-product-factory-live-plan-authority-v2"
_LIVE_AUTHORITY_KEY = secrets.token_bytes(32)
_DURABLE_WORKER_DIAGNOSTIC_OMITTED = "worker diagnostic omitted from durable checkpoint"
_DURABLE_REVIEW_REASON_OMITTED = "review rationale omitted from durable checkpoint"
_DURABLE_REVIEW_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|[\s?&#;,{\[(])['\"]?"
    r"(?:"
    r"x[-_]?api[-_]?key|api[-_]?key|subscription[-_]?key|"
    r"client[-_]?secret|password|passwd|secret|auth|authorization|"
    r"access[-_]?token|refresh[-_]?token|oauth(?:[-_]?token)?|"
    r"cookie|set[-_]?cookie|session(?:[-_]?(?:id|token))?|"
    r"awsaccesskeyid|x[-_]?amz[-_]?signature"
    r")[\'\"]?\s*(?:=|:)",
    re.IGNORECASE,
)


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
    # diagnostic metadata; the keyed proof binds the exact host-issued live checkpoint.
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

    def __post_init__(self) -> None:
        # Free-form worker diagnostics and reviewer-controlled review text are runtime
        # evidence, not durable authority. Normalize them before signing or persistence.
        object.__setattr__(
            self,
            "coordinator",
            _minimize_durable_worker_diagnostics(self.coordinator),
        )


def verify_live_checkpoint_authority(
    checkpoint: ProductProjectCoordinatorCheckpoint,
) -> str:
    """Verify the process-ephemeral host binding proof for an authority boundary.

    The proof binds both immutable plan authority and the exact live checkpoint snapshot.
    It is intentionally not durable. The checkpoint host uses it for initial anchor
    establishment and for the first durable state of a new repair generation; durable
    restart authority still comes from the independently persisted host-task anchor.
    Merely knowing or recomputing the public plan fingerprint cannot mint this keyed proof.
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
        coordinator=checkpoint.coordinator,
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

    Every live checkpoint receives a process-ephemeral keyed proof for the immutable
    trusted plan and exact snapshot. The proof cannot be reconstructed from checkpoint
    bytes or from the public plan fingerprint alone. The checkpoint host consumes it only
    at security-significant boundaries: first-anchor establishment and first durable
    `ready` state for a new repair generation. Restart authority subsequently comes from
    the independently persisted host-task anchor.
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
                coordinator=checkpoint.coordinator,
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


def _minimize_durable_worker_diagnostics(
    snapshot: CoordinatorSnapshot,
) -> CoordinatorSnapshot:
    records = tuple(_minimize_durable_work_record(record) for record in snapshot.records)
    if records == snapshot.records:
        return snapshot
    return replace(snapshot, records=records)


def _minimize_durable_work_record(record: WorkRecord) -> WorkRecord:
    result = record.result
    if result is None:
        return record

    coding_result = result.coding_result
    recovery = coding_result.recovery_state
    failure = coding_result.failure
    review = record.review
    safe_recovery = (
        None if recovery is None else replace(recovery, opaque_token=None)
    )
    safe_failure = (
        None
        if failure is None
        else replace(failure, message=_DURABLE_WORKER_DIAGNOSTIC_OMITTED)
    )
    safe_review = (
        None
        if review is None
        else replace(
            review,
            reason=_durable_review_reason(review.reason),
            evidence_refs=tuple(
                _durable_review_evidence_ref(value) for value in review.evidence_refs
            ),
        )
    )
    if review is not None and not review.accepted and record.blocker is not None:
        safe_blocker = _durable_review_reason(record.blocker)
    elif failure is not None and record.blocker is not None:
        safe_blocker = _DURABLE_WORKER_DIAGNOSTIC_OMITTED
    else:
        safe_blocker = record.blocker
    if (
        safe_recovery == recovery
        and safe_failure == failure
        and safe_review == review
        and safe_blocker == record.blocker
    ):
        return record

    safe_coding_result = replace(
        coding_result,
        recovery_state=safe_recovery,
        failure=safe_failure,
    )
    return replace(
        record,
        result=replace(result, coding_result=safe_coding_result),
        review=safe_review,
        blocker=safe_blocker,
    )


def _durable_review_reason(value: str) -> str:
    if (
        safe_evidence_reference(value) != value
        or _reference_has_credential_assignment(value)
        or _reference_has_url_userinfo(value)
    ):
        return _DURABLE_REVIEW_REASON_OMITTED
    return value


def _durable_review_evidence_ref(value: str) -> str:
    safe_reference = safe_evidence_reference(value)
    if safe_reference != value:
        return safe_reference
    if not (_reference_has_credential_assignment(value) or _reference_has_url_userinfo(value)):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _reference_has_credential_assignment(value: str) -> bool:
    return _DURABLE_REVIEW_CREDENTIAL_ASSIGNMENT.search(unquote(value)) is not None


def _reference_has_url_userinfo(value: str) -> bool:
    if "://" not in value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None


def _sign_live_authority(
    *,
    project_id: str,
    spec_version: int,
    row_version: int,
    fingerprint: str,
    coordinator: CoordinatorSnapshot,
) -> str:
    payload = json.dumps(
        (
            _LIVE_AUTHORITY_SCHEMA,
            project_id,
            spec_version,
            row_version,
            fingerprint,
            _authority_value(coordinator),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_LIVE_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()


def _authority_value(value: Any) -> Any:
    """Project an in-memory checkpoint value into deterministic proof framing.

    This framing is used only to authenticate a live host-issued capability in the same
    process. Durable checkpoint serialization remains owned by the checkpoint host.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _authority_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Enum):
        return _authority_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): _authority_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_authority_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_authority_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ProductProjectBindingError(
        f"unsupported live checkpoint authority value type: {type(value).__name__}"
    )
