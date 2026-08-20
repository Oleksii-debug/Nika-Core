from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ProductOperationsError(ValueError):
    """Raised when PF3 product-operations invariants are violated."""


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
        if self.wave < 0 or not self.replicas:
            raise ProductOperationsError("service wave/replicas are invalid")
        replica_ids = [r.replica_id for r in self.replicas]
        if len(replica_ids) != len(set(replica_ids)):
            raise ProductOperationsError("duplicate replica identity")
        if not 1 <= self.min_healthy_replicas <= len(self.replicas):
            raise ProductOperationsError("minimum healthy replicas is invalid")
        if self.service_id in self.dependencies:
            raise ProductOperationsError("service cannot depend on itself")
        for refs in (self.dependencies, self.credential_refs):
            if len(refs) != len(set(refs)) or any(not ref.strip() for ref in refs):
                raise ProductOperationsError("service references are invalid")


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
        if not self.service_id.strip() or not self.evidence_refs or healthy & failed:
            raise ProductOperationsError("service observation is invalid")
        if len(healthy) != len(self.healthy_replica_ids) or len(failed) != len(self.failed_replica_ids):
            raise ProductOperationsError("duplicate observed replica")


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
        if not self.service_id.strip() or not self.evidence_refs:
            raise ProductOperationsError("rollback observation is invalid")


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
        if not self.evidence_refs or (self.approval_ref is not None and not self.approval_ref.strip()):
            raise ProductOperationsError("maintenance evidence/approval is invalid")


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    applied: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.evidence_refs or (self.applied and self.uncertain):
            raise ProductOperationsError("maintenance result is invalid")


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
