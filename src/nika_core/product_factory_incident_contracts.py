from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .toolsmith.contracts import AcceptanceCommand, normalize_relative_path

INCIDENT_LIFECYCLE_SCHEMA_V1 = "nika-pf3-incident-repair-release-v1"
INCIDENT_LIFECYCLE_SCHEMA = "nika-pf3-incident-repair-release-v2"
INCIDENT_TRIGGER_FINGERPRINT_SCHEMA = INCIDENT_LIFECYCLE_SCHEMA_V1
SUPPORTED_INCIDENT_LIFECYCLE_SCHEMAS = frozenset(
    {INCIDENT_LIFECYCLE_SCHEMA_V1, INCIDENT_LIFECYCLE_SCHEMA}
)


class ProductIncidentError(ValueError):
    """Raised when PF3 incident/repair/release invariants are violated."""


class IncidentKind(StrEnum):
    HEALTH = "health"
    ERROR = "error"
    SECURITY = "security"
    DEPENDENCY = "dependency"
    OPERATOR = "operator"


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentState(StrEnum):
    OPEN = "open"
    PLANNED = "planned"
    REVIEW_REQUIRED = "review_required"
    RELEASE_READY = "release_ready"
    RECONCILE_REQUIRED = "reconcile_required"
    RESOLVED = "resolved"
    ROLLED_BACK = "rolled_back"


class ReleaseDisposition(StrEnum):
    HEALTHY = "healthy"
    ROLLED_BACK = "rolled_back"
    UNCERTAIN = "uncertain"


class ServiceDefinitionView(Protocol):
    service_id: str
    project_id: str
    environment_id: str
    release_sha: str


class ServiceObservationView(Protocol):
    evidence_refs: tuple[str, ...]


class RollbackObservationView(Protocol):
    evidence_refs: tuple[str, ...]


class ServiceRecordView(Protocol):
    service: ServiceDefinitionView
    observation: ServiceObservationView | None
    rollback: RollbackObservationView | None


class MaintenanceRequestView(Protocol):
    service_id: str


class MaintenanceResultView(Protocol):
    evidence_refs: tuple[str, ...]


class MaintenanceRecordView(Protocol):
    request: MaintenanceRequestView
    result: MaintenanceResultView


class OperationsSnapshotView(Protocol):
    project_id: str
    services: tuple[ServiceRecordView, ...]
    maintenance_records: tuple[MaintenanceRecordView, ...]


@dataclass(frozen=True, slots=True)
class SupplyChainAdvisory:
    advisory_id: str
    ecosystem: str
    package_name: str
    affected_version: str
    fixed_version: str | None
    provenance_ref: str

    def __post_init__(self) -> None:
        _nonempty(
            self.advisory_id,
            self.ecosystem,
            self.package_name,
            self.affected_version,
            self.provenance_ref,
            label="supply-chain advisory identity",
        )
        _optional_ref(self.fixed_version, "fixed_version")


@dataclass(frozen=True, slots=True)
class IncidentTrigger:
    project_id: str
    service_id: str
    environment_id: str
    release_sha: str
    kind: IncidentKind
    severity: IncidentSeverity
    evidence_refs: tuple[str, ...]
    approval_ref: str
    observed_at: datetime
    advisory: SupplyChainAdvisory | None = None

    def __post_init__(self) -> None:
        _nonempty(
            self.project_id,
            self.service_id,
            self.environment_id,
            self.approval_ref,
            label="incident trigger identity",
        )
        _sha(self.release_sha, "release_sha")
        _aware(self.observed_at)
        _refs(self.evidence_refs, "incident evidence")
        if type(self.kind) is not IncidentKind:
            raise ProductIncidentError("incident kind must be an IncidentKind")
        if type(self.severity) is not IncidentSeverity:
            raise ProductIncidentError("incident severity must be an IncidentSeverity")
        if self.advisory is not None and type(self.advisory) is not SupplyChainAdvisory:
            raise ProductIncidentError("incident advisory must be a SupplyChainAdvisory")
        if self.kind in {IncidentKind.SECURITY, IncidentKind.DEPENDENCY}:
            if self.advisory is None:
                raise ProductIncidentError(
                    "security/dependency incident requires supply-chain advisory evidence"
                )
            if self.advisory.provenance_ref not in self.evidence_refs:
                raise ProductIncidentError(
                    "supply-chain advisory provenance must be incident evidence"
                )
        elif self.advisory is not None:
            raise ProductIncidentError(
                "supply-chain advisory is only valid for security/dependency incidents"
            )

    @property
    def fingerprint(self) -> str:
        advisory = None
        if self.advisory is not None:
            advisory = {
                "advisory_id": self.advisory.advisory_id,
                "ecosystem": self.advisory.ecosystem,
                "package_name": self.advisory.package_name,
                "affected_version": self.advisory.affected_version,
                "fixed_version": self.advisory.fixed_version,
                "provenance_ref": self.advisory.provenance_ref,
            }
        payload = {
            "schema": INCIDENT_TRIGGER_FINGERPRINT_SCHEMA,
            "project_id": self.project_id,
            "service_id": self.service_id,
            "environment_id": self.environment_id,
            "release_sha": self.release_sha,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "evidence_refs": sorted(self.evidence_refs),
            "advisory": advisory,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class RepairWorkOrder:
    work_order_id: str
    incident_id: str
    project_id: str
    service_id: str
    repository_id: str
    component_id: str
    base_release_sha: str
    goal: str
    allowed_paths: tuple[str, ...]
    permission_ceiling: frozenset[str]
    acceptance_commands: tuple[tuple[str, ...], ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime
    advisory_id: str | None = None
    target_fixed_version: str | None = None

    def __post_init__(self) -> None:
        _nonempty(
            self.work_order_id,
            self.incident_id,
            self.project_id,
            self.service_id,
            self.repository_id,
            self.component_id,
            self.goal,
            label="repair work-order identity",
        )
        _sha(self.base_release_sha, "base_release_sha")
        _aware(self.created_at)
        _refs(self.allowed_paths, "allowed paths")
        for path in self.allowed_paths:
            _relative_path(path)
        if type(self.permission_ceiling) is not frozenset or not self.permission_ceiling:
            raise ProductIncidentError(
                "repair work order requires normalized non-empty permission ceiling"
            )
        for permission in self.permission_ceiling:
            _exact_text(permission, "repair work order permission")
            if permission != permission.strip():
                raise ProductIncidentError(
                    "repair work order requires normalized non-empty permission ceiling"
                )
        _refs(self.evidence_refs, "repair work-order evidence")
        _optional_ref(self.advisory_id, "work-order advisory_id")
        _optional_ref(self.target_fixed_version, "work-order target_fixed_version")
        if type(self.acceptance_commands) is not tuple or not self.acceptance_commands:
            raise ProductIncidentError("repair work order requires acceptance commands")
        for command in self.acceptance_commands:
            _acceptance_command(command)


@dataclass(frozen=True, slots=True)
class RepairCandidateEvidence:
    candidate_id: str
    incident_id: str
    work_order_id: str
    base_release_sha: str
    result_sha: str
    artifact_digest: str
    diff_digest: str
    regression_evidence_refs: tuple[str, ...]
    provenance_evidence_refs: tuple[str, ...]
    review_ref: str
    review_accepted: bool
    recorded_at: datetime

    def __post_init__(self) -> None:
        _nonempty(
            self.candidate_id,
            self.incident_id,
            self.work_order_id,
            self.review_ref,
            label="repair candidate identity",
        )
        _sha(self.base_release_sha, "base_release_sha")
        _sha(self.result_sha, "result_sha")
        _digest(self.artifact_digest, "artifact_digest")
        _digest(self.diff_digest, "diff_digest")
        _refs(self.regression_evidence_refs, "regression evidence")
        for evidence_digest in self.regression_evidence_refs:
            _digest(evidence_digest, "regression evidence digest")
        _refs(self.provenance_evidence_refs, "candidate provenance evidence")
        if type(self.review_accepted) is not bool:
            raise ProductIncidentError("repair candidate review_accepted must be boolean")
        _aware(self.recorded_at)


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    release_event_id: str
    incident_id: str
    candidate_id: str
    previous_release_sha: str
    candidate_release_sha: str
    artifact_digest: str
    staging_intent_id: str
    production_intent_id: str
    disposition: ReleaseDisposition
    deployment_evidence_refs: tuple[str, ...]
    health_evidence_refs: tuple[str, ...]
    restored_release_sha: str | None
    reconciliation_ref: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _nonempty(
            self.release_event_id,
            self.incident_id,
            self.candidate_id,
            self.staging_intent_id,
            self.production_intent_id,
            label="release evidence identity",
        )
        _sha(self.previous_release_sha, "previous_release_sha")
        _sha(self.candidate_release_sha, "candidate_release_sha")
        _digest(self.artifact_digest, "artifact_digest")
        if self.staging_intent_id == self.production_intent_id:
            raise ProductIncidentError("release requires distinct staging and production intents")
        _refs(self.deployment_evidence_refs, "deployment evidence")
        _aware(self.observed_at)
        if type(self.disposition) is not ReleaseDisposition:
            raise ProductIncidentError("release disposition must be a ReleaseDisposition")

        if self.disposition is ReleaseDisposition.HEALTHY:
            _refs(self.health_evidence_refs, "healthy release evidence")
            if self.restored_release_sha is not None:
                raise ProductIncidentError("healthy release cannot carry rollback state")
            _optional_ref(self.reconciliation_ref, "reconciliation_ref")
        elif self.disposition is ReleaseDisposition.ROLLED_BACK:
            _refs(self.health_evidence_refs, "rollback verification evidence")
            if self.restored_release_sha is None:
                raise ProductIncidentError("rollback requires restored release identity")
            _sha(self.restored_release_sha, "restored_release_sha")
            if self.restored_release_sha != self.previous_release_sha:
                raise ProductIncidentError("rollback must restore the exact known-good release")
            _optional_ref(self.reconciliation_ref, "reconciliation_ref")
        else:
            if self.health_evidence_refs:
                raise ProductIncidentError("uncertain release cannot claim health evidence")
            if self.restored_release_sha is not None:
                raise ProductIncidentError("uncertain release cannot claim restored release")
            if self.reconciliation_ref is not None:
                raise ProductIncidentError("uncertain release cannot claim reconciliation evidence")


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    incident_id: str
    trigger: IncidentTrigger
    state: IncidentState
    work_order: RepairWorkOrder | None = None
    candidates: tuple[RepairCandidateEvidence, ...] = ()
    release_events: tuple[ReleaseEvidence, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.incident_id, label="incident_id")
        if type(self.trigger) is not IncidentTrigger:
            raise ProductIncidentError("incident trigger must be an IncidentTrigger")
        if type(self.state) is not IncidentState:
            raise ProductIncidentError("incident state must be an IncidentState")
        if self.work_order is not None and type(self.work_order) is not RepairWorkOrder:
            raise ProductIncidentError("incident work_order must be a RepairWorkOrder")
        if type(self.candidates) is not tuple or any(
            type(item) is not RepairCandidateEvidence for item in self.candidates
        ):
            raise ProductIncidentError("incident candidates must be RepairCandidateEvidence values")
        if type(self.release_events) is not tuple or any(
            type(item) is not ReleaseEvidence for item in self.release_events
        ):
            raise ProductIncidentError("incident release_events must be ReleaseEvidence values")


@dataclass(frozen=True, slots=True)
class IncidentLifecycleSnapshot:
    schema: str
    project_id: str
    incidents: tuple[IncidentRecord, ...]
    fingerprint_index: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema) is not str
            or self.schema not in SUPPORTED_INCIDENT_LIFECYCLE_SCHEMAS
        ):
            raise ProductIncidentError("unsupported incident lifecycle snapshot schema")
        _nonempty(self.project_id, label="incident lifecycle snapshot project")
        if type(self.incidents) is not tuple or any(
            type(item) is not IncidentRecord for item in self.incidents
        ):
            raise ProductIncidentError("incident snapshot incidents must be IncidentRecord values")
        if type(self.fingerprint_index) is not tuple:
            raise ProductIncidentError("incident fingerprint index must be a tuple")
        for pair in self.fingerprint_index:
            if type(pair) is not tuple or len(pair) != 2:
                raise ProductIncidentError("incident fingerprint mapping must contain two values")
            _nonempty(pair[0], pair[1], label="incident fingerprint mapping")


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _aware(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ProductIncidentError("datetime must be a timezone-aware datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProductIncidentError("datetime must be timezone-aware")
        return value.astimezone(UTC)
    except ProductIncidentError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProductIncidentError("datetime must be representable in UTC") from exc


def _exact_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ProductIncidentError(f"{label} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductIncidentError(f"{label} must be valid UTF-8 text") from exc
    if not allow_empty and not value.strip():
        raise ProductIncidentError(f"{label} must not be empty")
    return value


def _nonempty(*values: str, label: str) -> None:
    for value in values:
        _exact_text(value, label)


def _refs(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple or not values:
        raise ProductIncidentError(f"{label} must not be empty")
    for value in values:
        _exact_text(value, label)
    if len(values) != len(set(values)):
        raise ProductIncidentError(f"{label} must not contain duplicates")


def _optional_ref(value: str | None, label: str) -> None:
    if value is not None:
        _exact_text(value, label)


def _relative_path(value: str) -> None:
    _exact_text(value, "allowed path")
    if value != value.strip():
        raise ProductIncidentError(
            "allowed path must be normalized project-relative non-.git scope"
        )
    try:
        normalize_relative_path(value)
    except ValueError as exc:
        raise ProductIncidentError(
            "allowed path must be normalized project-relative non-.git scope"
        ) from exc


def _acceptance_command(command: tuple[str, ...]) -> None:
    if type(command) is not tuple or not command:
        raise ProductIncidentError("repair acceptance command must be normalized and non-empty")
    for part in command:
        _exact_text(part, "repair acceptance command")
        if part != part.strip():
            raise ProductIncidentError("repair acceptance command must be normalized and non-empty")
    try:
        AcceptanceCommand(command)
    except ValueError as exc:
        raise ProductIncidentError(
            "repair acceptance command must use a non-shell Toolsmith entrypoint"
        ) from exc


def _sha(value: str, label: str) -> None:
    _exact_text(value, label)
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ProductIncidentError(f"{label} must be a lowercase 40-character hexadecimal SHA")


def validate_digest(value: str, label: str) -> None:
    """Validate a canonical lowercase SHA-256 digest used across PF8 modules."""

    _digest(value, label)


def _digest(value: str, label: str) -> None:
    _exact_text(value, label)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ProductIncidentError(f"{label} must be a lowercase 64-character hexadecimal digest")
