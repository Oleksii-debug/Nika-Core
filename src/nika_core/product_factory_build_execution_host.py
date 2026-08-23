from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from nika_core.product_factory_build_execution import (
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionNodePort,
    BuildExecutionPortError,
    BuildExecutionRecord,
    BuildExecutionResult,
    BuildExecutionSnapshot,
    BuildExecutionSpec,
    BuildExecutionState,
)
from nika_core.product_factory_build_execution_persistence import (
    BuildExecutionCheckpointIntegrityError,
    BuildExecutionDurabilityError,
    BuildFileEvidence,
    DurableBuildExecutionSnapshot,
    SQLiteBuildExecutionCheckpointStore,
    durable_state_fingerprint,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
    ExecutionRegistrySnapshot,
    Platform,
    WorkLease,
)
from nika_core.toolsmith.contracts import AllowedPathPolicy, ChangedFile, normalize_relative_path


@dataclass(frozen=True, slots=True)
class BuildOutputPolicy:
    """Trusted host-owned ceiling for repository changes produced by one build work item."""

    project_id: str
    repository_id: str
    work_id: str
    allowed_paths: AllowedPathPolicy
    max_changed_files: int
    path_identity: RepositoryPathIdentity

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.project_id, self.repository_id, self.work_id)):
            raise BuildExecutionDurabilityError("build output policy identity must not be empty")
        if type(self.max_changed_files) is not int or self.max_changed_files < 0:
            raise BuildExecutionDurabilityError(
                "build output max_changed_files must be a non-negative exact integer"
            )
        if not isinstance(self.path_identity, RepositoryPathIdentity):
            raise BuildExecutionDurabilityError("build output path identity must be explicit")


class TrustedBuildOutputPolicyPort(Protocol):
    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> BuildOutputPolicy: ...


class BuildExecutionFileEvidencePort(Protocol):
    """Read-only exact changed-file evidence collector for one completed dispatch."""

    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]: ...


@dataclass(slots=True)
class DurableBuildExecutionHost:
    """Production PF5 composition that makes state durable around every external effect.

    ``BuildExecutionCoordinator`` remains the provider-neutral state machine. This host is
    the effectful composition: all mutation methods checkpoint canonical SQLite state, and
    the node-port decorator checkpoints ``EFFECT_IN_FLIGHT`` before delegating ``run``.
    A persistence failure poisons this process-local host so callers cannot continue from
    state that may be newer than durable truth; recovery must construct a new host and use
    ``restore_latest``/inspection.
    """

    coordinator: BuildExecutionCoordinator
    node_port: BuildExecutionNodePort
    file_evidence_port: BuildExecutionFileEvidencePort
    output_policies: TrustedBuildOutputPolicyPort
    checkpoints: SQLiteBuildExecutionCheckpointStore
    _sequence: int = field(default=0, init=False, repr=False)
    _file_evidence: dict[str, BuildFileEvidence] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_fingerprint: str | None = field(default=None, init=False, repr=False)
    _poisoned: bool = field(default=False, init=False, repr=False)
    _needs_restore: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._needs_restore = self.checkpoints.has_checkpoint()
        if not self._needs_restore and self.coordinator.snapshot().records:
            raise BuildExecutionDurabilityError(
                "fresh durable PF5 host requires an empty coordinator before first submit"
            )

    def submit(
        self, spec: BuildExecutionSpec, *, now: datetime | None = None
    ) -> BuildExecutionRecord:
        self._ensure_usable()
        record = self.coordinator.submit(spec, now=now)
        self._persist_if_changed()
        return record

    def prepare(self, work_id: str, *, now: datetime | None = None) -> BuildExecutionRecord:
        self._ensure_usable()
        record = self.coordinator.prepare(work_id, now=now)
        self._persist_if_changed()
        return record

    def begin_dispatch(
        self, work_id: str, *, now: datetime | None = None
    ) -> BuildExecutionDispatch:
        self._ensure_usable()
        dispatch = self.coordinator.begin_dispatch(work_id, now=now)
        self._persist_if_changed()
        return dispatch

    def execute(self, work_id: str, *, now: datetime | None = None) -> BuildExecutionRecord:
        self._ensure_usable()
        port = _DurableNodePort(self)
        try:
            record = self.coordinator.run_dispatch(work_id, port, now=now)
        except Exception:
            # The decorator already persisted EFFECT_IN_FLIGHT before any real run call.
            # Unexpected provider/programming failure therefore remains inspection-only.
            raise
        self._persist_if_changed()
        return record

    def reconcile(self, work_id: str, *, now: datetime | None = None) -> BuildExecutionRecord:
        self._ensure_usable()
        port = _DurableNodePort(self)
        record = self.coordinator.reconcile(work_id, port, now=now)
        self._persist_if_changed()
        return record

    def retry(self, work_id: str, *, now: datetime | None = None) -> BuildExecutionRecord:
        self._ensure_usable()
        record = self.coordinator.retry(work_id, now=now)
        self._persist_if_changed()
        return record

    def restore_latest(self, *, now: datetime | None = None) -> DurableBuildExecutionSnapshot:
        if self._poisoned:
            raise BuildExecutionDurabilityError(
                "poisoned PF5 host must be discarded before durable recovery"
            )
        saved = self.checkpoints.latest()
        self._sequence = saved.snapshot.sequence
        self._last_fingerprint = durable_state_fingerprint(saved.snapshot)
        self._file_evidence = {
            item.work_id: item for item in saved.snapshot.file_evidence
        }
        candidate = self._restore_registry_leases(saved.snapshot, now=now)
        self._validate_file_evidence(candidate.coordinator)
        self.coordinator.restore(candidate.coordinator, now=now)
        self._needs_restore = False
        self._persist_if_changed()
        return self._durable_snapshot(self._sequence)

    def snapshot(self) -> DurableBuildExecutionSnapshot:
        self._ensure_usable()
        return self._durable_snapshot(self._sequence)

    def _before_run(self, dispatch: BuildExecutionDispatch) -> None:
        record = self.coordinator.get(dispatch.work_id)
        if record.state is not BuildExecutionState.EFFECT_IN_FLIGHT or record.dispatch != dispatch:
            raise BuildExecutionDurabilityError(
                "external build run reached port without exact EFFECT_IN_FLIGHT identity"
            )
        self._persist_if_changed()

    def _before_inspect(self, dispatch: BuildExecutionDispatch) -> None:
        record = self.coordinator.get(dispatch.work_id)
        if (
            record.state is not BuildExecutionState.RECONCILE_REQUIRED
            or record.dispatch != dispatch
        ):
            raise BuildExecutionDurabilityError(
                "external build inspection requires exact RECONCILE_REQUIRED identity"
            )
        self._persist_if_changed()

    def _collect_file_evidence(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> None:
        if result.uncertain or result.source_sha != dispatch.source_sha:
            return
        policy = self._resolve_output_policy(dispatch)
        changed_files = self.file_evidence_port.collect(dispatch, result)
        validated = _validate_changed_files(policy, dispatch.platform, changed_files)
        self._file_evidence[dispatch.work_id] = BuildFileEvidence(
            dispatch_id=dispatch.dispatch_id,
            project_id=dispatch.project_id,
            repository_id=dispatch.grant.repository_id,
            work_id=dispatch.work_id,
            source_sha=dispatch.source_sha,
            platform=dispatch.platform,
            changed_files=validated,
        )

    def _resolve_output_policy(self, dispatch: BuildExecutionDispatch) -> BuildOutputPolicy:
        policy = self.output_policies.resolve(
            project_id=dispatch.project_id,
            repository_id=dispatch.grant.repository_id,
            work_id=dispatch.work_id,
        )
        if (
            policy.project_id != dispatch.project_id
            or policy.repository_id != dispatch.grant.repository_id
            or policy.work_id != dispatch.work_id
        ):
            raise BuildExecutionDurabilityError(
                "trusted build output policy returned the wrong execution identity"
            )
        if dispatch.platform is Platform.WINDOWS and (
            policy.path_identity is not RepositoryPathIdentity.CASE_INSENSITIVE
        ):
            raise BuildExecutionDurabilityError(
                "Windows build output policy must declare case-insensitive path identity"
            )
        if dispatch.platform is Platform.LINUX and (
            policy.path_identity is not RepositoryPathIdentity.CASE_SENSITIVE
        ):
            raise BuildExecutionDurabilityError(
                "Linux build output policy must declare case-sensitive path identity"
            )
        return policy

    def _validate_file_evidence(self, snapshot: BuildExecutionSnapshot) -> None:
        records = {record.spec.request.work_id: record for record in snapshot.records}
        if set(self._file_evidence) - set(records):
            raise BuildExecutionCheckpointIntegrityError(
                "PF5 checkpoint contains orphan build file evidence"
            )
        for work_id, record in records.items():
            terminal = record.state in {BuildExecutionState.SUCCEEDED, BuildExecutionState.FAILED}
            evidence = self._file_evidence.get(work_id)
            if terminal and evidence is None:
                raise BuildExecutionCheckpointIntegrityError(
                    "terminal PF5 work lacks changed-file evidence"
                )
            if not terminal and evidence is not None:
                raise BuildExecutionCheckpointIntegrityError(
                    "non-terminal PF5 work contains changed-file evidence"
                )
            if evidence is None:
                continue
            if record.dispatch is None or (
                evidence.dispatch_id != record.dispatch.dispatch_id
                or evidence.project_id != record.spec.request.project_id
                or evidence.repository_id != record.spec.scope.repository_id
                or evidence.work_id != work_id
                or evidence.source_sha != record.spec.source_sha
                or evidence.platform is not record.spec.request.platform
            ):
                raise BuildExecutionCheckpointIntegrityError(
                    "PF5 changed-file evidence does not match exact durable dispatch"
                )
            policy = self._resolve_output_policy(record.dispatch)
            _validate_changed_files(policy, evidence.platform, evidence.changed_files)

    def _persist_if_changed(self) -> None:
        candidate = self._durable_snapshot(self._sequence + 1)
        fingerprint = durable_state_fingerprint(candidate)
        if self._last_fingerprint == fingerprint:
            return
        try:
            saved = self.checkpoints.save(candidate)
        except Exception:
            self._poisoned = True
            raise
        self._sequence = saved.snapshot.sequence
        self._last_fingerprint = durable_state_fingerprint(saved.snapshot)

    def _durable_snapshot(self, sequence: int) -> DurableBuildExecutionSnapshot:
        coordinator_snapshot = self.coordinator.snapshot()
        leases, next_lease = _owned_leases(self.coordinator, coordinator_snapshot)
        file_evidence = tuple(self._file_evidence[key] for key in sorted(self._file_evidence))
        snapshot = DurableBuildExecutionSnapshot(
            sequence,
            coordinator_snapshot,
            leases,
            next_lease,
            file_evidence,
        )
        self._validate_file_evidence(coordinator_snapshot)
        return snapshot

    def _restore_registry_leases(
        self,
        snapshot: DurableBuildExecutionSnapshot,
        *,
        now: datetime | None,
    ) -> DurableBuildExecutionSnapshot:
        instant = _aware(now or datetime.now(UTC))
        records = {record.spec.request.work_id: record for record in snapshot.coordinator.records}
        current = self.coordinator.nodes.snapshot()
        current_by_lease = {lease.lease_id: lease for lease in current.leases}
        current_by_node = {lease.node_id: lease for lease in current.leases}
        current_by_work = {(lease.project_id, lease.work_id): lease for lease in current.leases}
        durable_leases: list[WorkLease] = []
        for lease in snapshot.leases:
            record = records.get(lease.work_id)
            if record is None or lease.project_id != record.spec.request.project_id:
                raise BuildExecutionCheckpointIntegrityError(
                    "durable PF5 lease has no matching execution record"
                )
            if record.lease_id != lease.lease_id or record.node_id != lease.node_id:
                raise BuildExecutionCheckpointIntegrityError(
                    "durable PF5 lease identity does not match execution record"
                )
            if record.state is BuildExecutionState.PREPARED and (
                lease.expires_at <= instant
                or not self.coordinator.node_availability.is_available(lease.node_id)
            ):
                continue
            exact = current_by_lease.get(lease.lease_id)
            if exact is not None:
                if exact != lease:
                    raise BuildExecutionDurabilityError(
                        "current execution registry reused a durable PF5 lease id"
                    )
                continue
            if lease.node_id in current_by_node:
                raise BuildExecutionDurabilityError(
                    "current execution registry node is leased by different work"
                )
            key = (lease.project_id, lease.work_id)
            if key in current_by_work:
                raise BuildExecutionDurabilityError(
                    "current execution registry already has a different work lease"
                )
            durable_leases.append(lease)
        merged = ExecutionRegistrySnapshot(
            current.nodes,
            tuple(sorted((*current.leases, *durable_leases), key=lambda item: item.lease_id)),
            max(current.next_lease, snapshot.registry_next_lease),
        )
        self.coordinator.nodes.restore(merged)

        # A PREPARED lease that is expired/unavailable must not be resurrected as ready.
        rewritten: list[BuildExecutionRecord] = []
        durable_by_id = {lease.lease_id: lease for lease in snapshot.leases}
        changed = False
        for record in snapshot.coordinator.records:
            if record.state is BuildExecutionState.PREPARED and record.lease_id is not None:
                lease = durable_by_id.get(record.lease_id)
                unavailable = lease is None or lease.expires_at <= instant
                if lease is not None and not unavailable:
                    unavailable = not self.coordinator.node_availability.is_available(lease.node_id)
                if unavailable:
                    if lease is not None:
                        try:
                            self.coordinator.nodes.release(lease.lease_id)
                        except Exception:
                            pass
                    record = replace(
                        record,
                        state=BuildExecutionState.WAITING_FOR_NODE,
                        node_id=None,
                        lease_id=None,
                        dispatch=None,
                        block_reason="prepared execution node/lease unavailable after restart",
                        updated_at=instant,
                    )
                    changed = True
            rewritten.append(record)
        if changed:
            snapshot = replace(
                snapshot,
                coordinator=BuildExecutionSnapshot(tuple(rewritten)),
                leases=tuple(
                    lease
                    for lease in snapshot.leases
                    if any(record.lease_id == lease.lease_id for record in rewritten)
                ),
            )
        return snapshot

    def _ensure_usable(self) -> None:
        if self._poisoned:
            raise BuildExecutionDurabilityError(
                "PF5 host persistence failed; discard host and restore durable state "
                "before continuing"
            )
        if self._needs_restore:
            raise BuildExecutionDurabilityError(
                "durable PF5 state exists; restore_latest is required before execution"
            )


@dataclass(slots=True)
class _DurableNodePort:
    host: DurableBuildExecutionHost

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.host._before_run(dispatch)
        result = self.host.node_port.run(dispatch)
        try:
            self.host._collect_file_evidence(dispatch, result)
        except BuildExecutionDurabilityError as exc:
            raise BuildExecutionPortError(
                "build changed-file evidence exceeded trusted host policy"
            ) from exc
        return result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.host._before_inspect(dispatch)
        result = self.host.node_port.inspect(dispatch)
        if result is None or result.uncertain:
            return result
        self.host._collect_file_evidence(dispatch, result)
        return result


def _owned_leases(
    coordinator: BuildExecutionCoordinator,
    snapshot: BuildExecutionSnapshot,
) -> tuple[tuple[WorkLease, ...], int]:
    registry = coordinator.nodes.snapshot()
    records = {
        (record.spec.request.project_id, record.spec.request.work_id): record
        for record in snapshot.records
    }
    owned: list[WorkLease] = []
    for lease in registry.leases:
        record = records.get((lease.project_id, lease.work_id))
        if record is None:
            continue
        if record.lease_id != lease.lease_id or record.node_id != lease.node_id:
            raise BuildExecutionDurabilityError(
                "PF5 coordinator/registry active lease identity diverged before checkpoint"
            )
        owned.append(lease)
    for record in snapshot.records:
        if record.lease_id is not None and not any(
            lease.lease_id == record.lease_id for lease in owned
        ):
            raise BuildExecutionDurabilityError(
                "PF5 active execution record lacks its registry lease before checkpoint"
            )
    return tuple(sorted(owned, key=lambda item: item.lease_id)), registry.next_lease


def _validate_changed_files(
    policy: BuildOutputPolicy,
    platform: Platform,
    changed_files: tuple[ChangedFile, ...],
) -> tuple[ChangedFile, ...]:
    if not isinstance(changed_files, tuple) or any(
        not isinstance(item, ChangedFile) for item in changed_files
    ):
        raise BuildExecutionDurabilityError("build changed-file evidence must be a typed tuple")
    if len(changed_files) > policy.max_changed_files:
        raise BuildExecutionDurabilityError("build exceeded trusted changed-file count budget")
    if (
        platform is Platform.WINDOWS
        and policy.path_identity is not RepositoryPathIdentity.CASE_INSENSITIVE
    ):
        raise BuildExecutionDurabilityError("Windows path identity policy is invalid")
    if (
        platform is Platform.LINUX
        and policy.path_identity is not RepositoryPathIdentity.CASE_SENSITIVE
    ):
        raise BuildExecutionDurabilityError("Linux path identity policy is invalid")
    seen: set[str] = set()
    normalized: list[ChangedFile] = []
    roots = tuple(normalize_relative_path(root).as_posix() for root in policy.allowed_paths.roots)
    for item in changed_files:
        canonical = normalize_relative_path(item.path).as_posix()
        identity = (
            canonical.casefold()
            if policy.path_identity is RepositoryPathIdentity.CASE_INSENSITIVE
            else canonical
        )
        if identity in seen:
            raise BuildExecutionDurabilityError("build repeated changed-file identity")
        seen.add(identity)
        candidate_parts = identity.split("/")
        allowed = False
        for root in roots:
            root_identity = (
                root.casefold()
                if policy.path_identity is RepositoryPathIdentity.CASE_INSENSITIVE
                else root
            )
            root_parts = root_identity.split("/")
            if candidate_parts[: len(root_parts)] == root_parts:
                allowed = True
                break
        if not allowed:
            raise BuildExecutionDurabilityError(
                f"build changed file is outside trusted output paths: {canonical}"
            )
        normalized.append(ChangedFile(canonical, item.sha256, item.size_bytes))
    return tuple(normalized)
