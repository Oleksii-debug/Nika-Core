from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

_MAX_TEXT_UTF8_BYTES = 4096
_MAX_REFERENCE_COUNT = 4096
_SINGLE_LINE_FORBIDDEN = frozenset(("\x85", "\u2028", "\u2029"))


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
        canonical_text(self.replica_id, "replica identity")
        canonical_text(self.node_id, "replica node identity")


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
        canonical_text(self.service_id, "service identity")
        canonical_text(self.project_id, "service project identity")
        canonical_text(self.environment_id, "service environment identity")
        validate_sha(self.release_sha)
        if (
            type(self.wave) is not int
            or self.wave < 0
            or type(self.replicas) is not tuple
            or not self.replicas
            or len(self.replicas) > _MAX_REFERENCE_COUNT
            or any(type(replica) is not ServiceReplica for replica in self.replicas)
        ):
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
        canonical_text(self.service_id, "service observation identity")
        validate_sha(self.release_sha)
        aware(self.observed_at)
        _refs(self.evidence_refs, "service observation evidence")
        _refs(self.healthy_replica_ids, "healthy replica ids", allow_empty=True)
        _refs(self.failed_replica_ids, "failed replica ids", allow_empty=True)
        healthy = set(self.healthy_replica_ids)
        failed = set(self.failed_replica_ids)
        if healthy & failed:
            raise ProductOperationsError("service observation is invalid")


@dataclass(frozen=True, slots=True)
class RollbackObservation:
    service_id: str
    failed_release_sha: str
    restored_release_sha: str
    succeeded: bool
    evidence_refs: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        canonical_text(self.service_id, "rollback service identity")
        validate_sha(self.failed_release_sha)
        validate_sha(self.restored_release_sha)
        aware(self.observed_at)
        if type(self.succeeded) is not bool:
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
        canonical_text(self.request_id, "maintenance request identity")
        canonical_text(self.service_id, "maintenance service identity")
        canonical_text(self.reason, "maintenance reason")
        if type(self.action) is not MaintenanceAction:
            raise ProductOperationsError("maintenance action must be MaintenanceAction")
        _refs(self.evidence_refs, "maintenance evidence")
        if self.approval_ref is not None:
            canonical_text(self.approval_ref, "maintenance approval reference")


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
        canonical_text(self.operation_key, "maintenance effect reservation identity")
        if type(self.created) is not bool:
            raise ProductOperationsError("maintenance effect reservation identity is invalid")
        if type(self.state) is not MaintenanceEffectState:
            raise ProductOperationsError("maintenance effect reservation state is invalid")
        if self.created and self.state is not MaintenanceEffectState.PENDING:
            raise ProductOperationsError(
                "created maintenance effect reservation must be pending"
            )
        if self.result is not None and type(self.result) is not MaintenanceResult:
            raise ProductOperationsError("maintenance effect reservation result is invalid")
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


def canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductOperationsError(f"{label} must be exact canonical bounded text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductOperationsError(
            f"{label} must be exact canonical bounded text"
        ) from exc
    if (
        len(encoded) > _MAX_TEXT_UTF8_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in _SINGLE_LINE_FORBIDDEN for character in value)
    ):
        raise ProductOperationsError(f"{label} must be exact canonical bounded text")
    return value


def validate_sha(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 40
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ProductOperationsError("release SHA must be a lowercase 40-character hex digest")


def aware(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ProductOperationsError("datetime must be exact timezone-aware datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProductOperationsError("datetime must be timezone-aware")
        return value.astimezone(UTC)
    except ProductOperationsError:
        raise
    except (OverflowError, ValueError) as exc:
        raise ProductOperationsError("datetime must be timezone-aware") from exc


def _refs(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if (
        type(values) is not tuple
        or len(values) > _MAX_REFERENCE_COUNT
        or (not allow_empty and not values)
    ):
        raise ProductOperationsError(f"{label} must not be empty or oversized")
    for value in values:
        try:
            canonical_text(value, label)
        except ProductOperationsError as exc:
            raise ProductOperationsError(f"{label} contains an invalid reference") from exc
    if len(values) != len(set(values)):
        raise ProductOperationsError(f"{label} must not contain duplicates")
