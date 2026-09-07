from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ProductOperationsError(ValueError):
    """Raised when PF8 product-operations invariants are violated."""


class ServiceHealth(StrEnum):
    PENDING = "pending"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    BLOCKED = "blocked"
    ROLLBACK_REQUIRED = "rollback_required"
    ROLLED_BACK = "rolled_back"


class MaintenanceAction(StrEnum):
    DRAIN = "drain"
    RESTART = "restart"
    RESUME = "resume"
    VERIFY = "verify"


class MaintenanceState(StrEnum):
    IDLE = "idle"
    DRAINING = "draining"
    RESTARTING = "restarting"
    VERIFYING = "verifying"
    PAUSED = "paused"


class MaintenanceEffectState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ServiceReplica:
    replica_id: str
    node_id: str

    def __post_init__(self) -> None:
        if not self.replica_id.strip() or not self.node_id.strip():
            raise ProductOperationsError("replica identity must not be empty")


@dataclass(frozen=True, slots=True)
class DeployableService:
    service_id: str
    project_id: str
    environment_id: str
    release_sha: str
    wave: int
    replicas: tuple[ServiceReplica, ...]
    min_healthy_replicas: int = 1
    dependencies: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(v.strip() for v in (self.service_id, self.project_id, self.environment_id)):
            raise ProductOperationsError("service identity must not be empty")
        validate_sha(self.release_sha)
        if type(self.wave) is not int or self.wave < 0 or not self.replicas:
            raise ProductOperationsError("service wave/replicas are invalid")
        replica_ids = [r.replica_id for r in self.replicas]
        if len(replica_ids) != len(set(replica_ids)):
            raise ProductOperationsError("duplicate replica identity")
        if (
            type(self.min_healthy_replicas) is not int
            or not 1 <= self.min_healthy_replicas <= len(self.replicas)
        ):
            raise ProductOperationsError("minimum healthy replicas is invalid")
        if self.service_id in self.dependencies:
            raise ProductOperationsError("service cannot depend on itself")
        for refs in (self.dependencies, self.credential_refs):
            _refs(refs, "service references", allow_empty=True)


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    service_id: str
    release_sha: str
    healthy_replica_ids: tuple[str, ...]
    failed_replica_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        validate_sha(self.release_sha)
        aware(self.observed_at)
        healthy = set(self.healthy_replica_ids)
        failed = set(self.failed_replica_ids)
        if not self.service_id.strip() or healthy & failed:
            raise ProductOperationsError("service observation is invalid")
        _refs(self.evidence_refs, "service observation evidence")
        _refs(self.healthy_replica_ids, "healthy replica ids", allow_empty=True)
        _refs(self.failed_replica_ids, "failed replica ids", allow_empty=True)


@dataclass(frozen=True, slots=True)
class RollbackObservation:
    service_id: str
    failed_release_sha: str
    restored_release_sha: str
    succeeded: bool
    evidence_refs: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        validate_sha(self.failed_release_sha)
        validate_sha(self.restored_release_sha)
        aware(self.observed_at)
        if not self.service_id.strip() or type(self.succeeded) is not bool:
            raise ProductOperationsError("rollback observation is invalid")
        _refs(self.evidence_refs, "rollback observation evidence")


@dataclass(frozen=True, slots=True)
class MaintenanceRequest:
    request_id: str
    service_id: str
    action: MaintenanceAction
    reason: str
    evidence_refs: tuple[str, ...]
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        if not all(v.strip() for v in (self.request_id, self.service_id, self.reason)):
            raise ProductOperationsError("maintenance identity/reason is invalid")
        if not isinstance(self.action, MaintenanceAction):
            raise ProductOperationsError("maintenance action must be MaintenanceAction")
        _refs(self.evidence_refs, "maintenance evidence")
        if self.approval_ref is not None and not self.approval_ref.strip():
            raise ProductOperationsError("maintenance evidence/approval is invalid")


class MaintenanceApprovalAuthorityPort(Protocol):
    """Host-owned resolver for an exact PF8 maintenance approval subject.

    This is a consumer boundary only. It does not issue/sign approvals and is intended to
    adapt the canonical M10 trusted approval authority when that authority is integrated.
    """

    def verify(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    applied: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.applied) is not bool or type(self.uncertain) is not bool:
            raise ProductOperationsError("maintenance result flags must be boolean")
        _refs(self.evidence_refs, "maintenance result evidence")
        if self.applied and self.uncertain:
            raise ProductOperationsError("maintenance result is invalid")


@dataclass(frozen=True, slots=True)
class MaintenanceEffectReservation:
    operation_key: str
    state: MaintenanceEffectState
    created: bool
    result: MaintenanceResult | None = None

    def __post_init__(self) -> None:
        if not self.operation_key.strip() or type(self.created) is not bool:
            raise ProductOperationsError("maintenance effect reservation identity is invalid")
        if not isinstance(self.state, MaintenanceEffectState):
            raise ProductOperationsError("maintenance effect reservation state is invalid")
        if self.state is MaintenanceEffectState.COMPLETED:
            if self.result is None:
                raise ProductOperationsError("completed maintenance effect lacks durable result")
        elif self.result is not None:
            raise ProductOperationsError("unresolved maintenance effect cannot carry result")


class MaintenanceEffectJournalPort(Protocol):
    """Durable pre-effect reservation boundary for one maintenance task host."""

    def lookup(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> MaintenanceEffectReservation | None: ...

    def reserve(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> MaintenanceEffectReservation: ...

    def complete(self, operation_key: str, result: MaintenanceResult) -> None: ...
    def mark_uncertain(self, operation_key: str) -> None: ...
    def reconcile(self, operation_key: str, result: MaintenanceResult) -> None: ...


class ProductOperationsPort(Protocol):
    def apply(self, request: MaintenanceRequest) -> MaintenanceResult: ...
    def inspect(self, request: MaintenanceRequest) -> MaintenanceResult: ...


def validate_sha(value: str) -> None:
    if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ProductOperationsError("release SHA must be a lowercase 40-character hex digest")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductOperationsError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _refs(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if not allow_empty and not values:
        raise ProductOperationsError(f"{label} must not be empty")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ProductOperationsError(f"{label} contains an invalid reference")
    if len(values) != len(set(values)):
        raise ProductOperationsError(f"{label} must not contain duplicates")