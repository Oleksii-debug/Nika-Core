from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_factory_work_ownership import (
    ProductFactoryWorkOwnership,
    WorkOwnershipError,
    WorkOwnershipLease,
)
from nika_core.runtime.idempotency import (
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)
from nika_core.toolsmith.contracts import RecoveryState

_OPERATION_TYPE = "product_factory.coding_worker"


class ProductFactoryProgramError(RuntimeError):
    """Raised when durable Product Factory execution cannot proceed safely."""


class ProgramWorkDisposition(StrEnum):
    REVIEW_REQUIRED = "review_required"
    REPAIR_REQUIRED = "repair_required"
    NEEDS_RECOVERY = "needs_recovery"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    BLOCKED_MISSING_WORKER_STATE = "blocked_missing_worker_state"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ProgramWorkOutcome:
    component_id: str
    work_id: str
    disposition: ProgramWorkDisposition
    state: WorkState
    operation_status: IdempotencyStatus | None
    detail: str


class ProductFactoryProgramWorkerPort(Protocol):
    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope: ...

    async def inspect(self, work_id: str) -> RecoveryState | None: ...

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope: ...


@dataclass(slots=True)
class ProductFactoryProgramHost:
    """Crash-consistent Product Factory host above bounded CodingWorker dispatch.

    Work ownership is a durable fence, not advisory metadata. The canonical ordering is:

    1. acquire exact `(project_id, work_id, owner_id, fence)` authority;
    2. in one writer transaction assert that fence, persist RUNNING, and reserve the
       idempotent external operation;
    3. after any bounded concurrency wait, revalidate/re-establish exact fence authority
       immediately before an external dispatch or recovery effect;
    4. reconcile returned evidence under the same fence and atomically persist the
       result checkpoint plus ledger completion;
    5. release the lease only after a terminal durable transition, or after durable
       UNCERTAIN has made duplicate dispatch impossible.

    SQLite transactions never span an external worker await.
    """

    store: SQLiteStore
    worker: ProductFactoryProgramWorkerPort
    idempotency: IdempotencyLedger | None = field(default=None, repr=False)
    ownership: ProductFactoryWorkOwnership | None = field(default=None, repr=False)
    owner_id: str = field(
        default_factory=lambda: f"program-host:{uuid.uuid4().hex}",
        repr=False,
    )
    lease_seconds: int = 300
    _checkpoints: ProductFactoryCheckpointHost = field(init=False, repr=False)
    _ledger: IdempotencyLedger = field(init=False, repr=False)
    _ownership: ProductFactoryWorkOwnership = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner_id, str)
            or not self.owner_id.strip()
            or self.owner_id != self.owner_id.strip()
        ):
            raise ValueError("owner_id must be canonical non-empty text")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._checkpoints = ProductFactoryCheckpointHost(self.store)
        self._ledger = self.idempotency or IdempotencyLedger(self.store)
        self._ownership = self.ownership or ProductFactoryWorkOwnership(self.store)

    def restore_latest(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
    ) -> ProductFactoryCoordinator:
        candidate = self._checkpoints.inspect_latest(
            host_task_id=host_task_id,
            binding=binding,
        )
        if candidate.disposition is not ProductFactoryRecoveryDisposition.RESUMABLE:
            raise ProductFactoryProgramError(
                f"Product Factory checkpoint is not resumable: {candidate.disposition.value}"
            )
        coordinator = self._checkpoints.restore_latest(
            host_task_id=host_task_id,
            binding=binding,
        )
        self.reconcile_durable_results(
            host_task_id=host_task_id,
            coordinator=coordinator,
        )
        return coordinator

    async def dispatch_ready(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        max_parallel: int = 4,
        max_count: int = 32,
    ) -> tuple[ProgramWorkOutcome, ...]:
        if max_parallel <= 0 or max_count <= 0:
            raise ValueError("max_parallel and max_count must be positive")

        ready = coordinator.ready_requests()[:max_count]
        if not ready:
            return ()

        leases: list[WorkOwnershipLease] = []
        before_start = coordinator.snapshot()
        try:
            for request in ready:
                leases.append(self._acquire(request))
            started = tuple(coordinator.start(request.component_id) for request in ready)
            reservations = self._checkpoint_and_reserve(
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
                requests=started,
                leases=tuple(leases),
            )
        except Exception:
            coordinator.restore(before_start)
            for lease in leases:
                self._release_best_effort(lease)
            raise

        lease_by_work = {lease.work_id: lease for lease in leases}
        semaphore = asyncio.Semaphore(max_parallel)
        outcomes = await asyncio.gather(
            *(
                self._dispatch_one(
                    semaphore=semaphore,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                    request=request,
                    lease=lease_by_work[request.work_id],
                    reservation=reservations[request.work_id],
                )
                for request in started
            )
        )
        return tuple(sorted(outcomes, key=lambda item: item.component_id))

    async def recover_running(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        max_parallel: int = 4,
    ) -> tuple[ProgramWorkOutcome, ...]:
        if max_parallel <= 0:
            raise ValueError("max_parallel must be positive")

        self.reconcile_durable_results(host_task_id=host_task_id, coordinator=coordinator)
        running = tuple(
            record
            for record in coordinator.snapshot().records
            if record.state is WorkState.RUNNING
        )
        if not running:
            return ()

        semaphore = asyncio.Semaphore(max_parallel)
        outcomes = await asyncio.gather(
            *(
                self._recover_one(
                    semaphore=semaphore,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                    record=record,
                )
                for record in running
            )
        )
        return tuple(sorted(outcomes, key=lambda item: item.component_id))

    def reconcile_durable_results(
        self,
        *,
        host_task_id: str,
        coordinator: ProductFactoryCoordinator,
    ) -> tuple[str, ...]:
        reconciled: list[str] = []
        for record in coordinator.snapshot().records:
            if record.result is None:
                continue
            operation_key = _operation_key(record.request)
            operation = self._ledger.get(operation_key)
            if operation is None:
                continue
            if (
                operation.task_id != host_task_id
                or operation.operation_type != _OPERATION_TYPE
                or operation.input_fingerprint != _request_fingerprint(record.request)
            ):
                raise ProductFactoryProgramError(
                    "durable result operation identity requires explicit reconciliation"
                )
            if operation.status not in {
                IdempotencyStatus.PENDING,
                IdempotencyStatus.UNCERTAIN,
            }:
                continue
            lease = self._acquire(record.request)
            try:
                result = _result_summary(record)
                with self.store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._assert_lease(connection, lease)
                    if operation.status is IdempotencyStatus.PENDING:
                        self._ledger.complete_with_connection(
                            connection,
                            operation_key,
                            result,
                        )
                    else:
                        self._ledger._set_status_with_connection(
                            connection,
                            operation_key,
                            IdempotencyStatus.COMPLETED,
                            result,
                            allow_uncertain_completion=True,
                        )
                reconciled.append(operation_key)
            finally:
                self._release_best_effort(lease)
        return tuple(reconciled)

    def review_and_checkpoint(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
        decision: ReviewDecision,
    ) -> WorkRecord:
        request = _request_for_component(coordinator, component_id)
        lease = self._acquire(request)
        before = coordinator.snapshot()
        try:
            updated = coordinator.review(component_id, decision)
            self._save_fenced(host_task_id, binding, coordinator, lease)
        except Exception:
            coordinator.restore(before)
            raise
        finally:
            self._release_best_effort(lease)
        return updated

    def prepare_repair_and_checkpoint(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
        base_sha: str,
        reason: str,
    ) -> ComponentWorkRequest:
        prior_request = _request_for_component(coordinator, component_id)
        lease = self._acquire(prior_request)
        before = coordinator.snapshot()
        try:
            request = coordinator.prepare_repair(component_id, base_sha=base_sha, reason=reason)
            self._save_fenced(host_task_id, binding, coordinator, lease)
        except Exception:
            coordinator.restore(before)
            raise
        finally:
            self._release_best_effort(lease)
        return request

    def block_and_checkpoint(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
        reason: str,
    ) -> WorkRecord:
        request = _request_for_component(coordinator, component_id)
        lease = self._acquire(request)
        before = coordinator.snapshot()
        try:
            updated = coordinator.block(component_id, reason)
            self._save_fenced(host_task_id, binding, coordinator, lease)
        except Exception:
            coordinator.restore(before)
            raise
        finally:
            self._release_best_effort(lease)
        return updated

    async def _dispatch_one(
        self,
        *,
        semaphore: asyncio.Semaphore,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
        reservation: tuple[IdempotencyRecord, bool],
    ) -> ProgramWorkOutcome:
        operation, created = reservation
        if not created:
            self._release_best_effort(lease)
            return _existing_operation_outcome(request, operation)

        async with semaphore:
            lease = self._reestablish_effect_authority(request, lease)
            try:
                envelope = await self.worker.dispatch(request)
            except asyncio.CancelledError:
                self._mark_uncertain_fenced(_operation_key(request), lease)
                self._release_best_effort(lease)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external worker failure
                self._mark_uncertain_fenced(_operation_key(request), lease)
                self._release_best_effort(lease)
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.UNCERTAIN,
                    IdempotencyStatus.UNCERTAIN,
                    f"worker dispatch did not return trusted evidence: {type(exc).__name__}",
                )

        return self._record_worker_result(
            host_task_id=host_task_id,
            binding=binding,
            coordinator=coordinator,
            request=request,
            operation_key=_operation_key(request),
            envelope=envelope,
            was_uncertain=False,
            lease=lease,
        )

    async def _recover_one(
        self,
        *,
        semaphore: asyncio.Semaphore,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        record: WorkRecord,
    ) -> ProgramWorkOutcome:
        request = record.request
        operation_key = _operation_key(request)
        lease = self._acquire(request)
        operation = self._ledger.get(operation_key)

        if operation is None:
            try:
                with self.store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._assert_lease(connection, lease)
                    operation, created = self._ledger.reserve_with_connection(
                        connection,
                        operation_key=operation_key,
                        task_id=host_task_id,
                        operation_type=_OPERATION_TYPE,
                        input_fingerprint=_request_fingerprint(request),
                    )
            except Exception:
                self._release_best_effort(lease)
                raise
            if not created:
                self._release_best_effort(lease)
                return _existing_operation_outcome(request, operation)
            async with semaphore:
                lease = self._reestablish_effect_authority(request, lease)
                try:
                    envelope = await self.worker.dispatch(request)
                except asyncio.CancelledError:
                    self._mark_uncertain_fenced(operation_key, lease)
                    self._release_best_effort(lease)
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._mark_uncertain_fenced(operation_key, lease)
                    self._release_best_effort(lease)
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.UNCERTAIN,
                        IdempotencyStatus.UNCERTAIN,
                        f"worker dispatch did not return trusted evidence: {type(exc).__name__}",
                    )
            return self._record_worker_result(
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
                request=request,
                operation_key=operation_key,
                envelope=envelope,
                was_uncertain=False,
                lease=lease,
            )

        if operation.task_id != host_task_id or operation.operation_type != _OPERATION_TYPE:
            self._release_best_effort(lease)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                operation.status,
                "worker operation belongs to a different Product Factory host task",
            )
        if operation.input_fingerprint != _request_fingerprint(request):
            self._release_best_effort(lease)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                operation.status,
                "worker operation fingerprint does not match durable request",
            )
        if operation.status is IdempotencyStatus.COMPLETED:
            self._release_best_effort(lease)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                operation.status,
                "completed worker operation is inconsistent with RUNNING coordinator state",
            )

        async with semaphore:
            lease = self._reestablish_effect_authority(request, lease)
            try:
                state = await self.worker.inspect(request.work_id)
            except asyncio.CancelledError:
                self._mark_uncertain_fenced(operation_key, lease)
                self._release_best_effort(lease)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external inspect failure
                self._mark_uncertain_fenced(operation_key, lease)
                self._release_best_effort(lease)
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.UNCERTAIN,
                    IdempotencyStatus.UNCERTAIN,
                    f"worker inspection did not return trusted state: {type(exc).__name__}",
                )
            if state is None:
                before = coordinator.snapshot()
                blocked = coordinator.block(
                    request.component_id,
                    "worker recovery state is unavailable; explicit reconciliation required",
                )
                try:
                    self._save_and_mark_uncertain(
                        host_task_id=host_task_id,
                        binding=binding,
                        coordinator=coordinator,
                        operation_key=operation_key,
                        lease=lease,
                    )
                except Exception:
                    coordinator.restore(before)
                    self._release_best_effort(lease)
                    raise
                self._release_best_effort(lease)
                return ProgramWorkOutcome(
                    component_id=request.component_id,
                    work_id=request.work_id,
                    disposition=ProgramWorkDisposition.BLOCKED_MISSING_WORKER_STATE,
                    state=blocked.state,
                    operation_status=IdempotencyStatus.UNCERTAIN,
                    detail="worker state is missing; duplicate execution is forbidden",
                )
            lease = self._reestablish_effect_authority(request, lease)
            try:
                envelope = await self.worker.recover(request, state)
            except asyncio.CancelledError:
                self._mark_uncertain_fenced(operation_key, lease)
                self._release_best_effort(lease)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external recovery failure
                self._mark_uncertain_fenced(operation_key, lease)
                self._release_best_effort(lease)
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.UNCERTAIN,
                    IdempotencyStatus.UNCERTAIN,
                    f"worker recovery did not return trusted evidence: {type(exc).__name__}",
                )

        return self._record_worker_result(
            host_task_id=host_task_id,
            binding=binding,
            coordinator=coordinator,
            request=request,
            operation_key=operation_key,
            envelope=envelope,
            was_uncertain=operation.status is IdempotencyStatus.UNCERTAIN,
            lease=lease,
        )

    def _record_worker_result(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        request: ComponentWorkRequest,
        operation_key: str,
        envelope: WorkerResultEnvelope,
        was_uncertain: bool,
        lease: WorkOwnershipLease,
    ) -> ProgramWorkOutcome:
        before = coordinator.snapshot()
        try:
            updated = coordinator.record_result(envelope)
            summary = _result_summary(updated)
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_lease(connection, lease)
                self._checkpoint_on_connection(
                    connection,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                )
                if was_uncertain:
                    self._ledger._set_status_with_connection(
                        connection,
                        operation_key,
                        IdempotencyStatus.COMPLETED,
                        summary,
                        allow_uncertain_completion=True,
                    )
                else:
                    self._ledger.complete_with_connection(
                        connection,
                        operation_key,
                        summary,
                    )
        except Exception as exc:  # noqa: BLE001 - external effect must become uncertain
            coordinator.restore(before)
            marker_detail = ""
            try:
                self._mark_uncertain_fenced(operation_key, lease)
            except Exception as marker_exc:  # noqa: BLE001 - PENDING remains replay-blocking
                marker_detail = f"; uncertainty marker failed: {type(marker_exc).__name__}"
            self._release_best_effort(lease)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.UNCERTAIN,
                IdempotencyStatus.UNCERTAIN,
                (
                    "worker evidence could not be durably reconciled: "
                    f"{type(exc).__name__}{marker_detail}"
                ),
            )

        self._release_best_effort(lease)
        disposition = (
            ProgramWorkDisposition.REVIEW_REQUIRED
            if updated.state is WorkState.REVIEW_REQUIRED
            else ProgramWorkDisposition.REPAIR_REQUIRED
        )
        return ProgramWorkOutcome(
            component_id=request.component_id,
            work_id=request.work_id,
            disposition=disposition,
            state=updated.state,
            operation_status=IdempotencyStatus.COMPLETED,
            detail="worker evidence and operation completion are atomically durable",
        )

    def _checkpoint_and_reserve(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        requests: tuple[ComponentWorkRequest, ...],
        leases: tuple[WorkOwnershipLease, ...],
    ) -> dict[str, tuple[IdempotencyRecord, bool]]:
        lease_by_work = {lease.work_id: lease for lease in leases}
        if set(lease_by_work) != {request.work_id for request in requests}:
            raise ProductFactoryProgramError("RUNNING transition requires exact lease per work item")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for request in requests:
                self._assert_lease(connection, lease_by_work[request.work_id])
            self._checkpoint_on_connection(
                connection,
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
            )
            reservations: dict[str, tuple[IdempotencyRecord, bool]] = {}
            for request in requests:
                reservations[request.work_id] = self._ledger.reserve_with_connection(
                    connection,
                    operation_key=_operation_key(request),
                    task_id=host_task_id,
                    operation_type=_OPERATION_TYPE,
                    input_fingerprint=_request_fingerprint(request),
                )
            return reservations

    def _save_fenced(
        self,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        lease: WorkOwnershipLease,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            self._checkpoint_on_connection(
                connection,
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
            )

    def _save_and_mark_uncertain(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        operation_key: str,
        lease: WorkOwnershipLease,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            self._checkpoint_on_connection(
                connection,
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
            )
            current = self._ledger._require_with_connection(connection, operation_key)
            if current.status is IdempotencyStatus.PENDING:
                self._ledger.mark_uncertain_with_connection(connection, operation_key)

    def _checkpoint_on_connection(
        self,
        connection,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
    ) -> None:
        borrowed = _BorrowedSQLiteStore(self.store, connection)
        ProductFactoryCheckpointHost(borrowed).save(
            host_task_id=host_task_id,
            checkpoint=binding.checkpoint(coordinator),
        )

    def _mark_uncertain_fenced(
        self,
        operation_key: str,
        lease: WorkOwnershipLease,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            current = self._ledger._require_with_connection(connection, operation_key)
            if current.status is IdempotencyStatus.PENDING:
                self._ledger.mark_uncertain_with_connection(connection, operation_key)

    def _acquire(self, request: ComponentWorkRequest) -> WorkOwnershipLease:
        try:
            return self._ownership.acquire(
                project_id=request.project_id,
                work_id=request.work_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
        except WorkOwnershipError as exc:
            raise ProductFactoryProgramError(
                f"Product Factory work ownership is unavailable for {request.work_id}: {exc}"
            ) from exc

    def _reestablish_effect_authority(
        self,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
    ) -> WorkOwnershipLease:
        """Fail closed or mint a new fence after a wait, before any external effect."""
        try:
            self._ownership.assert_owner(
                project_id=lease.project_id,
                work_id=lease.work_id,
                owner_id=lease.owner_id,
                fence=lease.fence,
            )
            return lease
        except WorkOwnershipError:
            pass
        try:
            replacement = self._ownership.acquire(
                project_id=request.project_id,
                work_id=request.work_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
        except WorkOwnershipError as exc:
            raise ProductFactoryProgramError(
                f"stale Product Factory authority cannot start external effect for {request.work_id}: {exc}"
            ) from exc
        self._ownership.assert_owner(
            project_id=replacement.project_id,
            work_id=replacement.work_id,
            owner_id=replacement.owner_id,
            fence=replacement.fence,
        )
        return replacement

    def _assert_lease(self, connection, lease: WorkOwnershipLease) -> None:
        self._ownership.assert_owner_in_transaction(
            connection,
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
        )

    def _release_best_effort(self, lease: WorkOwnershipLease) -> None:
        try:
            self._ownership.release(
                project_id=lease.project_id,
                work_id=lease.work_id,
                owner_id=lease.owner_id,
                fence=lease.fence,
            )
        except WorkOwnershipError:
            return


class _BorrowedSQLiteStore:
    """Thin transaction adapter; canonical checkpoint code keeps owning checkpoint semantics."""

    def __init__(self, source: SQLiteStore, connection) -> None:
        self.path = source.path
        self._connection = connection

    @contextmanager
    def connection(self) -> Iterator:
        yield self._connection


def _request_for_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> ComponentWorkRequest:
    try:
        return next(
            record.request
            for record in coordinator.snapshot().records
            if record.request.component_id == component_id
        )
    except StopIteration as exc:
        raise ProductFactoryProgramError(f"unknown Product Factory component: {component_id}") from exc


def _existing_operation_outcome(
    request: ComponentWorkRequest,
    operation: IdempotencyRecord,
) -> ProgramWorkOutcome:
    disposition = (
        ProgramWorkDisposition.NEEDS_RECOVERY
        if operation.status in {IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN}
        else ProgramWorkDisposition.NEEDS_RECONCILIATION
    )
    return ProgramWorkOutcome(
        component_id=request.component_id,
        work_id=request.work_id,
        disposition=disposition,
        state=WorkState.RUNNING,
        operation_status=operation.status,
        detail="existing durable worker operation forbids duplicate dispatch",
    )


def _outcome(
    request: ComponentWorkRequest,
    coordinator: ProductFactoryCoordinator,
    disposition: ProgramWorkDisposition,
    operation_status: IdempotencyStatus | None,
    detail: str,
) -> ProgramWorkOutcome:
    state = next(
        record.state
        for record in coordinator.snapshot().records
        if record.request.component_id == request.component_id
    )
    return ProgramWorkOutcome(
        component_id=request.component_id,
        work_id=request.work_id,
        disposition=disposition,
        state=state,
        operation_status=operation_status,
        detail=detail,
    )


def _operation_key(request: ComponentWorkRequest) -> str:
    return f"pf-worker:{request.work_id}"


def _request_fingerprint(request: ComponentWorkRequest) -> str:
    payload = {
        "work_id": request.work_id,
        "project_id": request.project_id,
        "component_id": request.component_id,
        "repository_id": request.repository_id,
        "goal": request.goal,
        "base_sha": request.base_sha,
        "allowed_paths": list(request.allowed_paths),
        "permission_ceiling": sorted(request.permission_ceiling),
        "acceptance_commands": [list(command) for command in request.acceptance_commands],
        "attempt": request.attempt,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_summary(record: WorkRecord) -> dict[str, object]:
    if record.result is None:
        raise ProductFactoryProgramError("durable worker result is required for completion")
    return {
        "work_id": record.request.work_id,
        "component_id": record.request.component_id,
        "repository_id": record.request.repository_id,
        "base_sha": record.result.base_sha,
        "result_sha": record.result.result_sha,
        "diff_digest": record.result.diff_digest,
        "state": record.state.value,
    }
