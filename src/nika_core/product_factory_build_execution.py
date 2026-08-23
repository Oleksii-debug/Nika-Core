from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NormalizedBuildEvidence,
    Platform,
    WorkLease,
)


class BuildExecutionError(ValueError):
    """Raised when PF5 build-execution invariants are violated."""


class BuildExecutionState(StrEnum):
    PENDING = "pending"
    WAITING_FOR_NODE = "waiting_for_node"
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    RECONCILE_REQUIRED = "reconcile_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionNodeAvailabilityPort(Protocol):
    def is_available(self, node_id: str) -> bool: ...


class BuildExecutionNodePort(Protocol):
    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult: ...

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None: ...


@dataclass(frozen=True, slots=True)
class ProjectExecutionAuthority:
    project_id: str
    repository_id: str
    workspace_relpath: str
    allowed_node_ids: tuple[str, ...]
    network_scopes: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.repository_id.strip():
            raise BuildExecutionError("execution authority identity must not be empty")
        normalized = _normalize_project_relpath(self.workspace_relpath)
        object.__setattr__(self, "workspace_relpath", normalized)
        _validate_unique_nonempty(self.allowed_node_ids, "allowed node id")
        if not self.allowed_node_ids:
            raise BuildExecutionError("execution authority requires at least one authorized node")
        _validate_unique_nonempty(self.network_scopes, "network scope")
        _validate_unique_nonempty(self.credential_refs, "credential reference")
        if any("*" in scope for scope in self.network_scopes):
            raise BuildExecutionError("network authority must not contain wildcard scopes")
        for credential_ref in self.credential_refs:
            if credential_ref != credential_ref.strip():
                raise BuildExecutionError("credential reference must not contain edge whitespace")
            if not credential_ref.startswith("credref:") or not credential_ref[8:].strip():
                raise BuildExecutionError(
                    "execution credentials must use non-empty opaque credref: references"
                )


@dataclass(frozen=True, slots=True)
class BuildExecutionSpec:
    request: ExecutionRequest
    source_sha: str
    argv: tuple[str, ...]
    authority: ProjectExecutionAuthority
    lease_seconds: int = 900

    def __post_init__(self) -> None:
        _validate_sha(self.source_sha)
        if not self.argv or not self.argv[0].strip():
            raise BuildExecutionError("execution argv must contain a non-empty executable")
        if self.request.project_id != self.authority.project_id:
            raise BuildExecutionError("execution request and authority project mismatch")
        if type(self.lease_seconds) is not int or self.lease_seconds <= 0:
            raise BuildExecutionError("execution node lease duration must be a positive integer")


@dataclass(frozen=True, slots=True)
class BuildExecutionDispatch:
    dispatch_id: str
    project_id: str
    work_id: str
    node_id: str
    platform: Platform
    source_sha: str
    argv: tuple[str, ...]
    authority: ProjectExecutionAuthority
    attempt: int

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.dispatch_id, self.project_id, self.work_id, self.node_id)
        ):
            raise BuildExecutionError("dispatch identity must not be empty")
        if self.project_id != self.authority.project_id:
            raise BuildExecutionError("dispatch authority project mismatch")
        _validate_sha(self.source_sha)
        if type(self.attempt) is not int or self.attempt <= 0:
            raise BuildExecutionError("dispatch attempt must be a positive integer")


@dataclass(frozen=True, slots=True)
class BuildExecutionResult:
    source_sha: str
    artifact_digest: str
    succeeded: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        _validate_sha(self.source_sha)
        _validate_digest(self.artifact_digest)
        _aware(self.completed_at)
        if type(self.succeeded) is not bool or type(self.uncertain) is not bool:
            raise BuildExecutionError("execution result status fields must be exact booleans")
        if not self.evidence_refs:
            raise BuildExecutionError("execution result requires evidence references")
        _validate_unique_nonempty(self.evidence_refs, "evidence reference")
        if self.uncertain and self.succeeded:
            raise BuildExecutionError("uncertain execution result cannot claim success")


@dataclass(frozen=True, slots=True)
class BuildExecutionRecord:
    spec: BuildExecutionSpec
    state: BuildExecutionState
    node_id: str | None = None
    lease_id: str | None = None
    dispatch: BuildExecutionDispatch | None = None
    evidence: NormalizedBuildEvidence | None = None
    attempt: int = 0
    block_reason: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or self.attempt < 0:
            raise BuildExecutionError("execution attempt must be a non-negative integer")
        _aware(self.updated_at)
        if self.node_id is not None and not self.node_id.strip():
            raise BuildExecutionError("execution node identity must not be empty")
        if self.lease_id is not None and not self.lease_id.strip():
            raise BuildExecutionError("execution lease identity must not be empty")
        if self.block_reason is not None and not self.block_reason.strip():
            raise BuildExecutionError("execution block reason must not be empty")


@dataclass(frozen=True, slots=True)
class BuildExecutionSnapshot:
    records: tuple[BuildExecutionRecord, ...]


@dataclass(slots=True)
class BuildExecutionCoordinator:
    nodes: ExecutionNodeRegistry
    node_availability: ExecutionNodeAvailabilityPort
    _records: dict[str, BuildExecutionRecord] = field(default_factory=dict, init=False, repr=False)
    _leases: dict[str, WorkLease] = field(default_factory=dict, init=False, repr=False)

    def submit(
        self,
        spec: BuildExecutionSpec,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionRecord:
        work_id = spec.request.work_id
        existing = self._records.get(work_id)
        if existing is not None:
            if existing.spec != spec:
                raise BuildExecutionError("work id conflicts with prior execution payload")
            return existing
        instant = _aware(now or datetime.now(UTC))
        record = BuildExecutionRecord(spec, BuildExecutionState.PENDING, updated_at=instant)
        self._records[work_id] = record
        return record

    def prepare(
        self,
        work_id: str,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(work_id)
        if record.state in {
            BuildExecutionState.PREPARED,
            BuildExecutionState.DISPATCHING,
            BuildExecutionState.RECONCILE_REQUIRED,
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        }:
            return record
        self._release_lease(work_id)
        lease = self._acquire_authorized_available(record, instant)
        if lease is None:
            return self._save(
                replace(
                    record,
                    state=BuildExecutionState.WAITING_FOR_NODE,
                    node_id=None,
                    lease_id=None,
                    dispatch=None,
                    attempt=record.attempt + 1,
                    block_reason=_unavailable_reason(record.spec.request),
                    updated_at=instant,
                )
            )
        self._leases[work_id] = lease
        return self._save(
            replace(
                record,
                state=BuildExecutionState.PREPARED,
                node_id=lease.node_id,
                lease_id=lease.lease_id,
                dispatch=None,
                attempt=record.attempt + 1,
                block_reason=None,
                updated_at=instant,
            )
        )

    def begin_dispatch(
        self,
        work_id: str,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionDispatch:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(work_id)
        if record.state is BuildExecutionState.DISPATCHING and record.dispatch is not None:
            return record.dispatch
        if record.state is not BuildExecutionState.PREPARED:
            raise BuildExecutionError("execution work must be prepared before dispatch")
        lease = self._leases.get(work_id)
        if lease is None or lease.lease_id != record.lease_id or lease.node_id != record.node_id:
            raise BuildExecutionError("prepared execution work lost its exact node lease")
        if lease.expires_at <= instant:
            self._release_lease(work_id)
            self._save(
                replace(
                    record,
                    state=BuildExecutionState.WAITING_FOR_NODE,
                    node_id=None,
                    lease_id=None,
                    block_reason="prepared execution node lease expired before dispatch",
                    updated_at=instant,
                )
            )
            raise BuildExecutionError("execution node lease expired before dispatch")
        if not self.node_availability.is_available(lease.node_id):
            self._release_lease(work_id)
            self._save(
                replace(
                    record,
                    state=BuildExecutionState.WAITING_FOR_NODE,
                    node_id=None,
                    lease_id=None,
                    block_reason=(
                        f"selected execution node {lease.node_id} was lost before dispatch"
                    ),
                    updated_at=instant,
                )
            )
            raise BuildExecutionError("selected execution node became unavailable before dispatch")
        dispatch = BuildExecutionDispatch(
            dispatch_id=f"dispatch:{record.spec.request.project_id}:{work_id}:{record.attempt}",
            project_id=record.spec.request.project_id,
            work_id=work_id,
            node_id=lease.node_id,
            platform=record.spec.request.platform,
            source_sha=record.spec.source_sha,
            argv=record.spec.argv,
            authority=record.spec.authority,
            attempt=record.attempt,
        )
        self._save(
            replace(
                record,
                state=BuildExecutionState.DISPATCHING,
                dispatch=dispatch,
                block_reason=None,
                updated_at=instant,
            )
        )
        return dispatch

    def run_dispatch(
        self,
        work_id: str,
        port: BuildExecutionNodePort,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(work_id)
        if record.state is not BuildExecutionState.DISPATCHING or record.dispatch is None:
            return record
        try:
            result = port.run(record.dispatch)
        except Exception:
            self._release_lease(work_id)
            return self._save(
                replace(
                    record,
                    state=BuildExecutionState.RECONCILE_REQUIRED,
                    lease_id=None,
                    block_reason="execution dispatch outcome is uncertain after node-port failure",
                    updated_at=instant,
                )
            )
        return self._accept_result(record, result, instant)

    def reconcile(
        self,
        work_id: str,
        port: BuildExecutionNodePort,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(work_id)
        if record.state is not BuildExecutionState.RECONCILE_REQUIRED:
            return record
        if record.dispatch is None:
            raise BuildExecutionError("reconcile-required work lacks exact dispatch identity")
        result = port.inspect(record.dispatch)
        if result is None or result.uncertain:
            return record
        return self._accept_result(record, result, instant)

    def retry(
        self,
        work_id: str,
        *,
        now: datetime | None = None,
    ) -> BuildExecutionRecord:
        record = self._record(work_id)
        if record.state is not BuildExecutionState.WAITING_FOR_NODE:
            return record
        return self.prepare(work_id, now=now)

    def snapshot(self) -> BuildExecutionSnapshot:
        return BuildExecutionSnapshot(tuple(self._records[key] for key in sorted(self._records)))

    def restore(
        self,
        snapshot: BuildExecutionSnapshot,
        *,
        now: datetime | None = None,
    ) -> None:
        instant = _aware(now or datetime.now(UTC))
        work_ids = [record.spec.request.work_id for record in snapshot.records]
        if len(work_ids) != len(set(work_ids)):
            raise BuildExecutionError("execution snapshot contains duplicate work identities")
        registry_snapshot = self.nodes.snapshot()
        registry_nodes = {node.identity.node_id: node for node in registry_snapshot.nodes}
        registry_leases: dict[tuple[str, str], WorkLease] = {}
        for lease in registry_snapshot.leases:
            key = (lease.project_id, lease.work_id)
            if key in registry_leases:
                raise BuildExecutionError(
                    "execution registry contains duplicate active leases for one project/work"
                )
            registry_leases[key] = lease
        restored: dict[str, BuildExecutionRecord] = {}
        ephemeral: dict[str, WorkLease] = {}
        for record in snapshot.records:
            self._validate_snapshot_record(record)
            work_id = record.spec.request.work_id
            key = (record.spec.request.project_id, work_id)
            lease = registry_leases.get(key)
            current = record
            if record.state is BuildExecutionState.PREPARED:
                self._validate_active_lease_binding(record, lease, registry_nodes)
                if lease is None or lease.expires_at <= instant:
                    if lease is not None:
                        self._safe_registry_release(lease.lease_id)
                    current = replace(
                        record,
                        state=BuildExecutionState.WAITING_FOR_NODE,
                        node_id=None,
                        lease_id=None,
                        dispatch=None,
                        block_reason="prepared execution lease was not recoverable after restart",
                        updated_at=instant,
                    )
                else:
                    ephemeral[work_id] = lease
            elif record.state is BuildExecutionState.DISPATCHING:
                self._validate_active_lease_binding(record, lease, registry_nodes)
                if lease is not None:
                    self._safe_registry_release(lease.lease_id)
                current = replace(
                    record,
                    state=BuildExecutionState.RECONCILE_REQUIRED,
                    lease_id=None,
                    block_reason="restart crossed external dispatch boundary; inspection required",
                    updated_at=instant,
                )
            elif record.state is BuildExecutionState.RECONCILE_REQUIRED:
                if lease is not None:
                    self._safe_registry_release(lease.lease_id)
                current = replace(record, lease_id=None)
            elif lease is not None:
                raise BuildExecutionError(
                    "execution snapshot has an unexpected active lease for non-prepared work"
                )
            restored[work_id] = current
        self._records = restored
        self._leases = ephemeral

    def get(self, work_id: str) -> BuildExecutionRecord:
        return self._record(work_id)

    def _accept_result(
        self,
        record: BuildExecutionRecord,
        result: BuildExecutionResult,
        now: datetime,
    ) -> BuildExecutionRecord:
        dispatch = record.dispatch
        if dispatch is None:
            raise BuildExecutionError("execution result lacks exact dispatch binding")
        if result.source_sha != dispatch.source_sha:
            self._release_lease(dispatch.work_id)
            return self._save(
                replace(
                    record,
                    state=BuildExecutionState.RECONCILE_REQUIRED,
                    lease_id=None,
                    evidence=None,
                    block_reason=(
                        "execution result source SHA mismatch; exact inspection required"
                    ),
                    updated_at=now,
                )
            )
        self._release_lease(dispatch.work_id)
        if result.uncertain:
            return self._save(
                replace(
                    record,
                    state=BuildExecutionState.RECONCILE_REQUIRED,
                    lease_id=None,
                    evidence=None,
                    block_reason="execution node reported an uncertain result",
                    updated_at=now,
                )
            )
        evidence = NormalizedBuildEvidence(
            dispatch.work_id,
            dispatch.node_id,
            result.source_sha,
            result.artifact_digest,
            result.succeeded,
            result.evidence_refs,
        )
        return self._save(
            replace(
                record,
                state=(
                    BuildExecutionState.SUCCEEDED
                    if result.succeeded
                    else BuildExecutionState.FAILED
                ),
                lease_id=None,
                evidence=evidence,
                block_reason=None if result.succeeded else "execution node reported failure",
                updated_at=now,
            )
        )

    def _validate_snapshot_record(self, record: BuildExecutionRecord) -> None:
        _aware(record.updated_at)
        work_id = record.spec.request.work_id
        if record.spec.request.project_id != record.spec.authority.project_id:
            raise BuildExecutionError("snapshot execution authority project mismatch")
        if record.node_id is not None and record.node_id not in record.spec.authority.allowed_node_ids:
            raise BuildExecutionError("snapshot execution node is outside project authority")
        if record.dispatch is not None:
            dispatch = record.dispatch
            expected_dispatch_id = (
                f"dispatch:{record.spec.request.project_id}:{work_id}:{record.attempt}"
            )
            if (
                dispatch.dispatch_id != expected_dispatch_id
                or dispatch.project_id != record.spec.request.project_id
                or dispatch.work_id != work_id
                or dispatch.platform is not record.spec.request.platform
                or dispatch.source_sha != record.spec.source_sha
                or dispatch.argv != record.spec.argv
                or dispatch.authority != record.spec.authority
                or dispatch.attempt != record.attempt
                or dispatch.node_id != record.node_id
            ):
                raise BuildExecutionError(
                    "snapshot dispatch does not match execution specification"
                )
        if record.state in {
            BuildExecutionState.PENDING,
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.PREPARED,
        } and record.dispatch is not None:
            raise BuildExecutionError(
                "pre-dispatch execution snapshot must not contain a dispatch identity"
            )
        active_states = {BuildExecutionState.PREPARED, BuildExecutionState.DISPATCHING}
        if record.state in active_states:
            if record.node_id is None or record.lease_id is None:
                raise BuildExecutionError("active execution snapshot lacks node/lease identity")
        elif record.lease_id is not None:
            raise BuildExecutionError(
                "non-active execution snapshot must not retain a lease identity"
            )
        if record.state in {
            BuildExecutionState.DISPATCHING,
            BuildExecutionState.RECONCILE_REQUIRED,
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        } and record.dispatch is None:
            raise BuildExecutionError(
                "post-dispatch execution snapshot lacks exact dispatch identity"
            )
        if record.state in {BuildExecutionState.SUCCEEDED, BuildExecutionState.FAILED}:
            if record.evidence is None:
                raise BuildExecutionError("terminal execution snapshot lacks normalized evidence")
            if (
                record.evidence.work_id != work_id
                or record.evidence.node_id != record.node_id
                or record.evidence.release_sha != record.spec.source_sha
                or record.evidence.succeeded
                is not (record.state is BuildExecutionState.SUCCEEDED)
            ):
                raise BuildExecutionError(
                    "terminal execution evidence does not match durable record"
                )
        elif record.evidence is not None:
            raise BuildExecutionError(
                "non-terminal execution snapshot must not claim build evidence"
            )

    def _validate_active_lease_binding(
        self,
        record: BuildExecutionRecord,
        lease: WorkLease | None,
        registry_nodes: dict[str, ExecutionNode],
    ) -> None:
        if lease is None:
            return
        if lease.lease_id != record.lease_id or lease.node_id != record.node_id:
            raise BuildExecutionError(
                "execution snapshot active lease identity does not match registry"
            )
        node = registry_nodes.get(lease.node_id)
        if node is None:
            raise BuildExecutionError("execution snapshot active lease references unknown node")
        if not _node_satisfies_request(node, record.spec.request):
            raise BuildExecutionError(
                "execution snapshot active lease node no longer satisfies request contract"
            )

    def _acquire_authorized_available(
        self,
        record: BuildExecutionRecord,
        now: datetime,
    ) -> WorkLease | None:
        skipped: list[WorkLease] = []
        authorized = set(record.spec.authority.allowed_node_ids)
        selected: WorkLease | None = None
        try:
            while True:
                try:
                    candidate = self.nodes.acquire(
                        record.spec.request,
                        now=now,
                        lease_seconds=record.spec.lease_seconds,
                    )
                except DeploymentFabricError:
                    break
                if (
                    candidate.node_id in authorized
                    and self.node_availability.is_available(candidate.node_id)
                ):
                    selected = candidate
                    break
                skipped.append(candidate)
        finally:
            for candidate in skipped:
                self._safe_registry_release(candidate.lease_id)
        return selected

    def _release_lease(self, work_id: str) -> None:
        lease = self._leases.pop(work_id, None)
        if lease is not None:
            self._safe_registry_release(lease.lease_id)

    def _safe_registry_release(self, lease_id: str) -> None:
        try:
            self.nodes.release(lease_id)
        except DeploymentFabricError:
            pass

    def _record(self, work_id: str) -> BuildExecutionRecord:
        record = self._records.get(work_id)
        if record is None:
            raise BuildExecutionError("unknown build execution work")
        return record

    def _save(self, record: BuildExecutionRecord) -> BuildExecutionRecord:
        self._records[record.spec.request.work_id] = record
        return record


def _unavailable_reason(request: ExecutionRequest) -> str:
    gpu = " with GPU" if request.require_gpu else ""
    return f"no authorized {request.platform.value}{gpu} execution node satisfies requested scope"


def _normalize_project_relpath(value: str) -> str:
    if value != value.strip() or not value:
        raise BuildExecutionError("workspace path must be a non-empty project-relative path")
    portable = value.replace("\\", "/")
    if portable.startswith("/") or portable.startswith("//") or re.match(r"^[A-Za-z]:", portable):
        raise BuildExecutionError("workspace path must stay project-relative")
    parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BuildExecutionError("workspace path contains an unsafe segment")
    return "/".join(parts)


def _validate_unique_nonempty(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise BuildExecutionError(f"{label}s must be unique")
    if any(not value.strip() for value in values):
        raise BuildExecutionError(f"{label} must not be empty")


def _validate_sha(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise BuildExecutionError("source SHA must be exact lowercase 40-character hex")


def _validate_digest(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise BuildExecutionError("artifact digest must be exact lowercase SHA-256 hex")


def _node_satisfies_request(node: ExecutionNode, request: ExecutionRequest) -> bool:
    return (
        node.enabled
        and node.identity.platform is request.platform
        and node.resources.fits(request.resources)
        and request.required_features <= node.capabilities.features
        and request.required_toolchains <= node.capabilities.toolchains
        and (not request.require_gpu or node.capabilities.gpu)
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BuildExecutionError("execution datetime must be timezone-aware")
    return value.astimezone(UTC)
