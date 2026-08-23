from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol


class DeploymentFabricError(ValueError):
    """Raised when PF3 execution/deployment invariants are violated."""


class Platform(StrEnum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


class EnvironmentTier(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class DeploymentState(StrEnum):
    HEALTH_CHECK = "health_check"
    HEALTHY = "healthy"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    node_id: str
    platform: Platform
    architecture: str
    instance_id: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.node_id, self.architecture, self.instance_id)):
            raise DeploymentFabricError("node identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class NodeCapabilities:
    features: frozenset[str] = frozenset()
    toolchains: frozenset[str] = frozenset()
    gpu: bool = False


@dataclass(frozen=True, slots=True)
class ResourceEnvelope:
    cpu_cores: int
    memory_mb: int
    disk_mb: int

    def __post_init__(self) -> None:
        if min(self.cpu_cores, self.memory_mb, self.disk_mb) <= 0:
            raise DeploymentFabricError("resource envelope values must be positive")

    def fits(self, requested: ResourceEnvelope) -> bool:
        return (
            self.cpu_cores >= requested.cpu_cores
            and self.memory_mb >= requested.memory_mb
            and self.disk_mb >= requested.disk_mb
        )


@dataclass(frozen=True, slots=True)
class ExecutionNode:
    identity: NodeIdentity
    capabilities: NodeCapabilities
    resources: ResourceEnvelope
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    project_id: str
    work_id: str
    platform: Platform
    required_features: frozenset[str]
    required_toolchains: frozenset[str]
    resources: ResourceEnvelope
    require_gpu: bool = False

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.work_id.strip():
            raise DeploymentFabricError("execution request identity must not be empty")


@dataclass(frozen=True, slots=True)
class WorkLease:
    lease_id: str
    project_id: str
    work_id: str
    node_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NormalizedBuildEvidence:
    work_id: str
    node_id: str
    release_sha: str
    artifact_digest: str
    succeeded: bool
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sha(self.release_sha)
        _validate_digest(self.artifact_digest)
        if not self.work_id.strip() or not self.node_id.strip() or not self.evidence_refs:
            raise DeploymentFabricError("build evidence identity/refs must not be empty")


@dataclass(frozen=True, slots=True)
class ExecutionRegistrySnapshot:
    nodes: tuple[ExecutionNode, ...]
    leases: tuple[WorkLease, ...]
    next_lease: int


@dataclass(slots=True)
class ExecutionNodeRegistry:
    _nodes: dict[str, ExecutionNode] = field(default_factory=dict, init=False, repr=False)
    _leases: dict[str, WorkLease] = field(default_factory=dict, init=False, repr=False)
    _next_lease: int = field(default=1, init=False, repr=False)

    def register(self, node: ExecutionNode) -> None:
        existing = self._nodes.get(node.identity.node_id)
        if existing is not None and existing.identity != node.identity:
            raise DeploymentFabricError("node id already belongs to another node identity")
        self._nodes[node.identity.node_id] = node

    def acquire(
        self,
        request: ExecutionRequest,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> WorkLease:
        if lease_seconds <= 0:
            raise DeploymentFabricError("lease_seconds must be positive")
        instant = _aware(now or datetime.now(UTC))
        self._expire(instant)
        leased_nodes = {lease.node_id for lease in self._leases.values()}
        candidates = sorted(self._nodes.values(), key=lambda node: node.identity.node_id)
        for node in candidates:
            if node.identity.node_id in leased_nodes or not self._matches(node, request):
                continue
            lease = WorkLease(
                f"lease-{self._next_lease:08d}",
                request.project_id,
                request.work_id,
                node.identity.node_id,
                instant,
                instant + timedelta(seconds=lease_seconds),
            )
            self._next_lease += 1
            self._leases[lease.lease_id] = lease
            return lease
        raise DeploymentFabricError(
            f"no available execution node satisfies platform {request.platform.value} and scope"
        )

    def release(self, lease_id: str) -> None:
        if self._leases.pop(lease_id, None) is None:
            raise DeploymentFabricError("unknown work lease")

    def snapshot(self) -> ExecutionRegistrySnapshot:
        return ExecutionRegistrySnapshot(
            tuple(self._nodes[key] for key in sorted(self._nodes)),
            tuple(self._leases[key] for key in sorted(self._leases)),
            self._next_lease,
        )

    def restore(self, snapshot: ExecutionRegistrySnapshot) -> None:
        node_ids = [node.identity.node_id for node in snapshot.nodes]
        lease_ids = [lease.lease_id for lease in snapshot.leases]
        leased_nodes = [lease.node_id for lease in snapshot.leases]
        if len(node_ids) != len(set(node_ids)) or len(lease_ids) != len(set(lease_ids)):
            raise DeploymentFabricError("snapshot contains duplicate identities")
        if len(leased_nodes) != len(set(leased_nodes)):
            raise DeploymentFabricError("snapshot contains multiple active leases for one node")
        known_nodes = set(node_ids)
        if any(lease.node_id not in known_nodes for lease in snapshot.leases):
            raise DeploymentFabricError("snapshot lease references unknown node")
        for lease in snapshot.leases:
            if not all(
                value.strip()
                for value in (lease.lease_id, lease.project_id, lease.work_id, lease.node_id)
            ):
                raise DeploymentFabricError("snapshot lease identity must not be empty")
            issued_at = _aware(lease.issued_at)
            expires_at = _aware(lease.expires_at)
            if expires_at <= issued_at:
                raise DeploymentFabricError("snapshot lease expiry must be after issue time")
        if snapshot.next_lease < 1:
            raise DeploymentFabricError("snapshot next lease counter is invalid")
        self._nodes = {node.identity.node_id: node for node in snapshot.nodes}
        self._leases = {lease.lease_id: lease for lease in snapshot.leases}
        self._next_lease = snapshot.next_lease

    @staticmethod
    def _matches(node: ExecutionNode, request: ExecutionRequest) -> bool:
        return (
            node.enabled
            and node.identity.platform is request.platform
            and node.resources.fits(request.resources)
            and request.required_features <= node.capabilities.features
            and request.required_toolchains <= node.capabilities.toolchains
            and (not request.require_gpu or node.capabilities.gpu)
        )

    def _expire(self, now: datetime) -> None:
        for key in [key for key, lease in self._leases.items() if lease.expires_at <= now]:
            del self._leases[key]


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    environment_id: str
    project_id: str
    tier: EnvironmentTier
    provider_ref: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.environment_id, self.project_id, self.provider_ref)
        ):
            raise DeploymentFabricError("environment identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    project_id: str
    version: str
    source_sha: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.version.strip():
            raise DeploymentFabricError("release identity must not be empty")
        _validate_sha(self.source_sha)
        _validate_digest(self.artifact_digest)


@dataclass(frozen=True, slots=True)
class DeploymentIntent:
    intent_id: str
    project_id: str
    environment: EnvironmentIdentity
    release: ReleaseRef
    migration_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.project_id.strip():
            raise DeploymentFabricError("deployment intent identity must not be empty")
        if (
            self.project_id != self.environment.project_id
            or self.project_id != self.release.project_id
        ):
            raise DeploymentFabricError("deployment intent project identity mismatch")


@dataclass(frozen=True, slots=True)
class HealthEvidence:
    environment_id: str
    release_sha: str
    healthy: bool
    evidence_refs: tuple[str, ...]
    checked_at: datetime

    def __post_init__(self) -> None:
        _validate_sha(self.release_sha)
        _aware(self.checked_at)
        if not self.environment_id.strip() or not self.evidence_refs:
            raise DeploymentFabricError("health evidence identity/refs must not be empty")


@dataclass(frozen=True, slots=True)
class RollbackEvidence:
    environment_id: str
    failed_release_sha: str
    restored_release_sha: str | None
    succeeded: bool
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sha(self.failed_release_sha)
        if self.restored_release_sha is not None:
            _validate_sha(self.restored_release_sha)
        if not self.environment_id.strip() or not self.evidence_refs:
            raise DeploymentFabricError("rollback evidence identity/refs must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderDeploymentResult:
    applied: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderInspection:
    release_sha: str | None
    healthy: bool | None
    evidence_refs: tuple[str, ...]


class DeploymentProviderPort(Protocol):
    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult: ...

    def health(self, intent: DeploymentIntent) -> HealthEvidence: ...

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence: ...

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection: ...


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    intent: DeploymentIntent
    state: DeploymentState
    provider_evidence_refs: tuple[str, ...] = ()
    health: HealthEvidence | None = None
    rollback: RollbackEvidence | None = None
    previous_release_sha: str | None = None


@dataclass(frozen=True, slots=True)
class DeploymentFabricSnapshot:
    records: tuple[DeploymentRecord, ...]
    healthy_staging: tuple[tuple[str, ...], ...]
    current_releases: tuple[tuple[str, ...], ...]


@dataclass(slots=True)
class DeploymentFabric:
    provider: DeploymentProviderPort
    _records: dict[str, DeploymentRecord] = field(default_factory=dict, init=False, repr=False)
    _healthy_staging: dict[str, ReleaseRef] = field(default_factory=dict, init=False, repr=False)
    _current_releases: dict[tuple[str, str], str] = field(
        default_factory=dict, init=False, repr=False
    )

    def deploy(self, intent: DeploymentIntent) -> DeploymentRecord:
        existing = self._records.get(intent.intent_id)
        if existing is not None:
            if existing.intent != intent:
                raise DeploymentFabricError("deployment intent id conflicts with prior payload")
            return existing
        self._enforce_no_unresolved_effect(intent)
        self._enforce_staging_first(intent)
        environment_key = _environment_key(intent)
        previous = self._current_releases.get(environment_key)
        uncertain = DeploymentRecord(
            intent,
            DeploymentState.UNCERTAIN,
            previous_release_sha=previous,
        )
        try:
            result = self.provider.deploy(intent)
        except Exception:  # noqa: BLE001
            # A provider may mutate the target before failing to return a result.
            return self._mark_uncertain(uncertain)
        if not result.evidence_refs:
            return self._mark_uncertain(uncertain)
        if result.uncertain:
            return self._mark_uncertain(
                DeploymentRecord(
                    intent,
                    DeploymentState.UNCERTAIN,
                    result.evidence_refs,
                    previous_release_sha=previous,
                )
            )
        if not result.applied:
            return self._save(
                DeploymentRecord(
                    intent,
                    DeploymentState.REJECTED,
                    result.evidence_refs,
                    previous_release_sha=previous,
                )
            )
        record = DeploymentRecord(
            intent,
            DeploymentState.HEALTH_CHECK,
            result.evidence_refs,
            previous_release_sha=previous,
        )
        try:
            health = self.provider.health(intent)
        except Exception:  # noqa: BLE001
            # Provider failures after an applied effect must become durable uncertainty.
            return self._mark_uncertain(record)
        try:
            return self._finish_health(record, health)
        except DeploymentFabricError:
            return self._mark_uncertain(record, health.evidence_refs)

    def reconcile(self, intent_id: str) -> DeploymentRecord:
        record = self._record(intent_id)
        if record.state is not DeploymentState.UNCERTAIN:
            return record
        inspection = self.provider.inspect(record.intent)
        if not inspection.evidence_refs:
            raise DeploymentFabricError("provider inspection requires evidence refs")
        if inspection.release_sha is None:
            self._current_releases.pop(_environment_key(record.intent), None)
            return self._save(
                DeploymentRecord(
                    record.intent,
                    DeploymentState.REJECTED,
                    record.provider_evidence_refs + inspection.evidence_refs,
                    previous_release_sha=record.previous_release_sha,
                )
            )
        if inspection.release_sha != record.intent.release.source_sha:
            self._mark_uncertain(record, inspection.evidence_refs)
            raise DeploymentFabricError(
                "provider reports a different release for uncertain deployment"
            )
        if inspection.healthy is None:
            return self._mark_uncertain(record, inspection.evidence_refs)
        health = HealthEvidence(
            record.intent.environment.environment_id,
            record.intent.release.source_sha,
            inspection.healthy,
            inspection.evidence_refs,
            datetime.now(UTC),
        )
        return self._finish_health(record, health)

    def snapshot(self) -> DeploymentFabricSnapshot:
        current_releases = tuple(
            (project_id, environment_id, source_sha)
            for (project_id, environment_id), source_sha in sorted(
                self._current_releases.items()
            )
        )
        healthy_staging = tuple(
            (
                project_id,
                release.version,
                release.source_sha,
                release.artifact_digest,
            )
            for project_id, release in sorted(self._healthy_staging.items())
        )
        return DeploymentFabricSnapshot(
            tuple(self._records[key] for key in sorted(self._records)),
            healthy_staging,
            current_releases,
        )

    def restore(self, snapshot: DeploymentFabricSnapshot) -> None:
        ids = [record.intent.intent_id for record in snapshot.records]
        if len(ids) != len(set(ids)):
            raise DeploymentFabricError("deployment snapshot contains duplicate intents")
        for record in snapshot.records:
            _validate_record(record)

        healthy_staging: dict[str, ReleaseRef] = {}
        for entry in snapshot.healthy_staging:
            project_id, release = _normalize_healthy_staging_entry(entry, snapshot.records)
            if project_id in healthy_staging:
                raise DeploymentFabricError("deployment snapshot contains duplicate staging state")
            if not _has_healthy_staging_record(snapshot.records, release):
                raise DeploymentFabricError(
                    "healthy staging snapshot is not backed by a healthy staging record"
                )
            if _has_unresolved_staging_record(snapshot.records, project_id):
                raise DeploymentFabricError(
                    "healthy staging snapshot conflicts with unresolved staging effect"
                )
            healthy_staging[project_id] = release

        current_releases: dict[tuple[str, str], str] = {}
        for entry in snapshot.current_releases:
            project_id, environment_id, source_sha = _normalize_current_release_entry(
                entry, snapshot.records
            )
            key = (project_id, environment_id)
            if key in current_releases:
                raise DeploymentFabricError(
                    "deployment snapshot contains duplicate current release state"
                )
            if not _has_healthy_environment_record(
                snapshot.records, project_id, environment_id, source_sha
            ):
                raise DeploymentFabricError(
                    "current release snapshot is not backed by a healthy deployment record"
                )
            current_releases[key] = source_sha

        self._records = {record.intent.intent_id: record for record in snapshot.records}
        self._healthy_staging = healthy_staging
        self._current_releases = current_releases

    def _finish_health(
        self, record: DeploymentRecord, health: HealthEvidence
    ) -> DeploymentRecord:
        intent = record.intent
        if health.environment_id != intent.environment.environment_id:
            raise DeploymentFabricError("health evidence environment mismatch")
        if health.release_sha != intent.release.source_sha:
            raise DeploymentFabricError("health evidence release mismatch")
        if health.healthy:
            updated = DeploymentRecord(
                intent,
                DeploymentState.HEALTHY,
                record.provider_evidence_refs,
                health=health,
                previous_release_sha=record.previous_release_sha,
            )
            self._current_releases[_environment_key(intent)] = intent.release.source_sha
            if intent.environment.tier is EnvironmentTier.STAGING:
                self._healthy_staging[intent.project_id] = intent.release
            return self._save(updated)
        try:
            rollback = self.provider.rollback(intent, record.previous_release_sha)
        except Exception:  # noqa: BLE001
            # Provider failures after an applied effect must become durable uncertainty.
            return self._mark_uncertain(record, health.evidence_refs)
        if rollback.environment_id != intent.environment.environment_id:
            raise DeploymentFabricError("rollback evidence environment mismatch")
        if rollback.failed_release_sha != intent.release.source_sha:
            raise DeploymentFabricError("rollback evidence failed release mismatch")
        if rollback.succeeded and rollback.restored_release_sha != record.previous_release_sha:
            raise DeploymentFabricError(
                "rollback success did not restore the recorded previous release"
            )
        if not rollback.succeeded:
            return self._mark_uncertain(
                record,
                health.evidence_refs + rollback.evidence_refs,
            )
        return self._save(
            DeploymentRecord(
                intent,
                DeploymentState.ROLLED_BACK,
                record.provider_evidence_refs,
                health=health,
                rollback=rollback,
                previous_release_sha=record.previous_release_sha,
            )
        )

    def _enforce_no_unresolved_effect(self, intent: DeploymentIntent) -> None:
        environment_key = _environment_key(intent)
        if any(
            record.state is DeploymentState.UNCERTAIN
            and _environment_key(record.intent) == environment_key
            for record in self._records.values()
        ):
            raise DeploymentFabricError(
                "environment has an unresolved deployment effect"
            )

    def _enforce_staging_first(self, intent: DeploymentIntent) -> None:
        if (
            intent.environment.tier is EnvironmentTier.PRODUCTION
            and self._healthy_staging.get(intent.project_id) != intent.release
        ):
            raise DeploymentFabricError(
                "production deploy requires healthy staging proof for exact release"
            )

    def _mark_uncertain(
        self,
        record: DeploymentRecord,
        evidence_refs: tuple[str, ...] = (),
    ) -> DeploymentRecord:
        if record.intent.environment.tier is EnvironmentTier.STAGING:
            self._healthy_staging.pop(record.intent.project_id, None)
        return self._save(
            DeploymentRecord(
                record.intent,
                DeploymentState.UNCERTAIN,
                record.provider_evidence_refs + evidence_refs,
                previous_release_sha=record.previous_release_sha,
            )
        )

    def _record(self, intent_id: str) -> DeploymentRecord:
        try:
            return self._records[intent_id]
        except KeyError as exc:
            raise DeploymentFabricError("unknown deployment intent") from exc

    def _save(self, record: DeploymentRecord) -> DeploymentRecord:
        self._records[record.intent.intent_id] = record
        return record


def local_windows_node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity("local-windows", Platform.WINDOWS, "x86_64", "local-windows-1"),
        NodeCapabilities(
            frozenset({"local", "package"}), frozenset({"python", "powershell"})
        ),
        ResourceEnvelope(2, 2048, 4096),
    )


def local_linux_node() -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity("local-linux", Platform.LINUX, "x86_64", "local-linux-1"),
        NodeCapabilities(
            frozenset({"local", "container"}), frozenset({"python", "bash"})
        ),
        ResourceEnvelope(2, 2048, 4096),
    )


def _environment_key(intent: DeploymentIntent) -> tuple[str, str]:
    return intent.project_id, intent.environment.environment_id


def _normalize_healthy_staging_entry(
    entry: tuple[str, ...], records: tuple[DeploymentRecord, ...]
) -> tuple[str, ReleaseRef]:
    if len(entry) == 4:
        project_id, version, source_sha, artifact_digest = entry
        release = ReleaseRef(project_id, version, source_sha, artifact_digest)
        return project_id, release
    if len(entry) == 2:
        project_id, source_sha = entry
        if not project_id.strip():
            raise DeploymentFabricError("staging snapshot project identity must not be empty")
        _validate_sha(source_sha)
        matches = {
            record.intent.release
            for record in records
            if record.state is DeploymentState.HEALTHY
            and record.intent.project_id == project_id
            and record.intent.environment.tier is EnvironmentTier.STAGING
            and record.intent.release.source_sha == source_sha
        }
        if len(matches) != 1:
            raise DeploymentFabricError(
                "legacy staging snapshot is ambiguous or not backed by one exact release"
            )
        return project_id, next(iter(matches))
    raise DeploymentFabricError("healthy staging snapshot entry has invalid shape")


def _normalize_current_release_entry(
    entry: tuple[str, ...], records: tuple[DeploymentRecord, ...]
) -> tuple[str, str, str]:
    if len(entry) == 3:
        project_id, environment_id, source_sha = entry
        if not project_id.strip() or not environment_id.strip():
            raise DeploymentFabricError("current release snapshot identity must not be empty")
        _validate_sha(source_sha)
        return project_id, environment_id, source_sha
    if len(entry) == 2:
        environment_id, source_sha = entry
        if not environment_id.strip():
            raise DeploymentFabricError("current release snapshot identity must not be empty")
        _validate_sha(source_sha)
        matches = {
            record.intent.project_id
            for record in records
            if record.state is DeploymentState.HEALTHY
            and record.intent.environment.environment_id == environment_id
            and record.intent.release.source_sha == source_sha
        }
        if len(matches) != 1:
            raise DeploymentFabricError(
                "legacy current release snapshot is ambiguous or not backed by one project"
            )
        return next(iter(matches)), environment_id, source_sha
    raise DeploymentFabricError("current release snapshot entry has invalid shape")


def _has_healthy_staging_record(
    records: tuple[DeploymentRecord, ...], release: ReleaseRef
) -> bool:
    return any(
        record.state is DeploymentState.HEALTHY
        and record.intent.project_id == release.project_id
        and record.intent.environment.tier is EnvironmentTier.STAGING
        and record.intent.release == release
        for record in records
    )


def _has_unresolved_staging_record(
    records: tuple[DeploymentRecord, ...],
    project_id: str,
) -> bool:
    return any(
        record.state is DeploymentState.UNCERTAIN
        and record.intent.project_id == project_id
        and record.intent.environment.tier is EnvironmentTier.STAGING
        for record in records
    )


def _has_healthy_environment_record(
    records: tuple[DeploymentRecord, ...],
    project_id: str,
    environment_id: str,
    source_sha: str,
) -> bool:
    return any(
        record.state is DeploymentState.HEALTHY
        and record.intent.project_id == project_id
        and record.intent.environment.environment_id == environment_id
        and record.intent.release.source_sha == source_sha
        for record in records
    )


def _validate_record(record: DeploymentRecord) -> None:
    intent = record.intent
    if record.previous_release_sha is not None:
        _validate_sha(record.previous_release_sha)
    if not record.provider_evidence_refs and record.state is not DeploymentState.UNCERTAIN:
        raise DeploymentFabricError("deployment snapshot record requires provider evidence refs")
    if record.health is not None:
        if record.health.environment_id != intent.environment.environment_id:
            raise DeploymentFabricError("snapshot health evidence environment mismatch")
        if record.health.release_sha != intent.release.source_sha:
            raise DeploymentFabricError("snapshot health evidence release mismatch")
    if record.rollback is not None:
        if record.rollback.environment_id != intent.environment.environment_id:
            raise DeploymentFabricError("snapshot rollback evidence environment mismatch")
        if record.rollback.failed_release_sha != intent.release.source_sha:
            raise DeploymentFabricError("snapshot rollback evidence failed release mismatch")
        if (
            record.rollback.succeeded
            and record.rollback.restored_release_sha != record.previous_release_sha
        ):
            raise DeploymentFabricError(
                "snapshot rollback success did not restore recorded previous release"
            )

    if record.state is DeploymentState.HEALTHY:
        if record.health is None or not record.health.healthy or record.rollback is not None:
            raise DeploymentFabricError("healthy snapshot record is semantically inconsistent")
    elif record.state is DeploymentState.ROLLED_BACK:
        if (
            record.health is None
            or record.health.healthy
            or record.rollback is None
            or not record.rollback.succeeded
        ):
            raise DeploymentFabricError("rolled-back snapshot record is semantically inconsistent")
    elif record.state is DeploymentState.HEALTH_CHECK:
        raise DeploymentFabricError("health-check state must not be serialized as durable")
    elif record.state is DeploymentState.UNCERTAIN and (
        record.health is not None or record.rollback is not None
    ):
        raise DeploymentFabricError("uncertain snapshot record has final evidence")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DeploymentFabricError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise DeploymentFabricError("release SHA must be a lowercase 40-character hex SHA")


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DeploymentFabricError("artifact digest must be a lowercase 64-character hex digest")
