from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeVar

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
    WorkOwnershipConflictError,
    WorkOwnershipError,
    WorkOwnershipLease,
)
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)
from nika_core.toolsmith.contracts import RecoveryState

_OPERATION_TYPE = "product_factory.coding_worker"
_EFFECT_CANCEL_GRACE_SECONDS = 0.1
_MAX_RECOVERY_STATE_TEXT_UTF8_BYTES = 4096
_T = TypeVar("_T")


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
    2. under that fence, persist the READY -> RUNNING transition without yet claiming
       that an external worker effect has been admitted;
    3. after the bounded concurrency wait, revalidate the exact fence and reserve the
       idempotent operation immediately before the external dispatch/recovery effect;
       lost authority fails closed and canonical recovery owns the durable work;
    4. reconcile returned evidence under the same fence and atomically persist the
       result checkpoint plus ledger completion;
    5. release the lease after terminal durable reconciliation, after durable UNCERTAIN
       has made replay recovery-safe, or on a proven pre-effect exit where no worker
       effect was admitted.

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
        if self.idempotency is not None and self.idempotency._store is not self.store:
            raise ValueError(
                "Product Factory idempotency must use the host SQLiteStore instance"
            )
        if self.ownership is not None and self.ownership._store is not self.store:
            raise ValueError(
                "Product Factory ownership must use the host SQLiteStore instance"
            )
        if (
            type(self.owner_id) is not str
            or not self.owner_id
            or self.owner_id != self.owner_id.strip()
        ):
            raise ValueError("owner_id must be exact canonical non-empty text")
        if type(self.lease_seconds) is not int or self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be an exact positive integer")
        self._checkpoints = ProductFactoryCheckpointHost(self.store)
        self._ledger = (
            IdempotencyLedger(self.store) if self.idempotency is None else self.idempotency
        )
        self._ownership = (
            ProductFactoryWorkOwnership(self.store)
            if self.ownership is None
            else self.ownership
        )

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
        if (
            type(max_parallel) is not int
            or max_parallel <= 0
            or type(max_count) is not int
            or max_count <= 0
        ):
            raise ValueError("max_parallel and max_count must be exact positive integers")

        ready = tuple(
            _snapshot_component_work_request(request)
            for request in coordinator.ready_requests()[:max_count]
        )
        if not ready:
            return ()

        leases: list[WorkOwnershipLease] = []
        admitted: list[ComponentWorkRequest] = []
        deferred: list[ProgramWorkOutcome] = []
        before_start = coordinator.snapshot()
        try:
            for request in ready:
                lease = self._acquire_if_available(request)
                if lease is None:
                    deferred.append(
                        _outcome(
                            request,
                            coordinator,
                            ProgramWorkDisposition.NEEDS_RECONCILIATION,
                            None,
                            (
                                "active Product Factory ownership forbids duplicate dispatch; "
                                "independent ready work may continue"
                            ),
                        )
                    )
                    continue
                leases.append(lease)
                admitted.append(request)
            started_list: list[ComponentWorkRequest] = []
            for request in admitted:
                started_request = _snapshot_component_work_request(
                    coordinator.start(request.component_id)
                )
                if started_request != request:
                    raise ProductFactoryProgramError(
                        "Product Factory request authority changed before dispatch"
                    )
                started_list.append(started_request)
            started = tuple(started_list)
            if started:
                self._checkpoint_running(
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
        outcomes = await _settle_work_batch(
            tuple(
                self._dispatch_one(
                    semaphore=semaphore,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                    request=request,
                    lease=lease_by_work[request.work_id],
                )
                for request in started
            )
        )
        return tuple(
            sorted((*deferred, *outcomes), key=lambda item: item.component_id)
        )

    async def recover_running(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        max_parallel: int = 4,
    ) -> tuple[ProgramWorkOutcome, ...]:
        if type(max_parallel) is not int or max_parallel <= 0:
            raise ValueError("max_parallel must be an exact positive integer")

        self.reconcile_durable_results(host_task_id=host_task_id, coordinator=coordinator)
        running = tuple(
            record
            for record in coordinator.snapshot().records
            if record.state is WorkState.RUNNING
        )
        if not running:
            return ()

        semaphore = asyncio.Semaphore(max_parallel)
        outcomes = await _settle_work_batch(
            tuple(
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
                raise ProductFactoryProgramError(
                    "durable worker result is missing its idempotency operation"
                )
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
            lease = self._acquire_if_available(record.request)
            if lease is None:
                continue
            try:
                result = _result_summary(record)
                with self.store.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._assert_lease(connection, lease)
                    self._clear_stale_recovery_claim_for_reconciliation(
                        connection,
                        operation_key,
                        lease,
                    )
                    try:
                        current = self._ledger._require_with_connection(
                            connection,
                            operation_key,
                        )
                    except KeyError as exc:
                        raise ProductFactoryProgramError(
                            "durable result operation disappeared during reconciliation"
                        ) from exc
                    if (
                        current.task_id != host_task_id
                        or current.operation_type != _OPERATION_TYPE
                        or current.input_fingerprint
                        != _request_fingerprint(record.request)
                    ):
                        raise ProductFactoryProgramError(
                            "durable result operation identity changed during reconciliation"
                        )
                    if current.status is IdempotencyStatus.COMPLETED:
                        continue
                    if current.status is IdempotencyStatus.PENDING:
                        self._ledger.complete_with_connection(
                            connection,
                            operation_key,
                            result,
                        )
                    elif current.status is IdempotencyStatus.UNCERTAIN:
                        self._ledger._set_status_with_connection(
                            connection,
                            operation_key,
                            IdempotencyStatus.COMPLETED,
                            result,
                            allow_uncertain_completion=True,
                        )
                    else:  # pragma: no cover - enum is currently exhaustive
                        raise ProductFactoryProgramError(
                            "durable result operation has unsupported reconciliation status"
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
    ) -> ProgramWorkOutcome:
        operation_key = _operation_key(request)
        try:
            lease = await self._wait_for_effect_admission(
                semaphore=semaphore,
                request=request,
                lease=lease,
            )
        except asyncio.CancelledError:
            self._release_best_effort(lease)
            raise
        except Exception:
            self._release_best_effort(lease)
            raise

        try:
            try:
                lease = self._reestablish_effect_authority(request, lease)
                try:
                    operation, created = self._reserve_effect(
                        host_task_id=host_task_id,
                        request=request,
                        lease=lease,
                    )
                except IdempotencyConflictError:
                    durable_status = self._matching_operation_status(
                        operation_key,
                        host_task_id=host_task_id,
                        request=request,
                    )
                    self._release_best_effort(lease)
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.NEEDS_RECONCILIATION,
                        durable_status,
                        "worker reservation identity requires explicit reconciliation",
                    )
            except Exception:
                self._release_best_effort(lease)
                raise

            if not created:
                if (
                    operation.task_id != host_task_id
                    or operation.operation_type != _OPERATION_TYPE
                    or operation.input_fingerprint != _request_fingerprint(request)
                ):
                    self._release_best_effort(lease)
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.NEEDS_RECONCILIATION,
                        None,
                        (
                            "existing worker operation has a different host "
                            "or request identity"
                        ),
                    )
                self._release_best_effort(lease)
                return _existing_operation_outcome(request, operation)

            try:
                envelope, lease = await self._run_effect_with_lease(
                    request,
                    lease,
                    self.worker.dispatch(_snapshot_component_work_request(request)),
                )
            except asyncio.CancelledError:
                self._mark_uncertain_with_status(
                    operation_key,
                    lease,
                    host_task_id=host_task_id,
                    request=request,
                )
                self._release_best_effort(lease)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external worker failure
                durable_status, marker_detail = self._mark_uncertain_with_status(
                    operation_key,
                    lease,
                    host_task_id=host_task_id,
                    request=request,
                )
                self._release_best_effort(lease)
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.UNCERTAIN,
                    durable_status,
                    (
                        "worker dispatch did not return trusted evidence: "
                        f"{type(exc).__name__}{marker_detail}"
                    ),
                )
        finally:
            semaphore.release()

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

    async def _recover_one(
        self,
        *,
        semaphore: asyncio.Semaphore,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        record: WorkRecord,
    ) -> ProgramWorkOutcome:
        request = _snapshot_component_work_request(record.request)
        operation_key = _operation_key(request)
        lease = self._acquire_if_available(request)
        if lease is None:
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                self._matching_operation_status(
                    operation_key,
                    host_task_id=host_task_id,
                    request=request,
                ),
                (
                    "active Product Factory ownership forbids duplicate recovery; "
                    "independent running work may continue"
                ),
            )
        try:
            operation = self._ledger.get(operation_key)

            if operation is None:
                lease = await self._wait_for_effect_admission(
                    semaphore=semaphore,
                    request=request,
                    lease=lease,
                )
                try:
                    lease = self._reestablish_effect_authority(request, lease)
                    try:
                        operation, created = self._reserve_effect(
                            host_task_id=host_task_id,
                            request=request,
                            lease=lease,
                        )
                    except IdempotencyConflictError:
                        durable_status = self._matching_operation_status(
                            operation_key,
                            host_task_id=host_task_id,
                            request=request,
                        )
                        return _outcome(
                            request,
                            coordinator,
                            ProgramWorkDisposition.NEEDS_RECONCILIATION,
                            durable_status,
                            "worker reservation identity requires explicit reconciliation",
                        )
                    if not created:
                        if (
                            operation.task_id != host_task_id
                            or operation.operation_type != _OPERATION_TYPE
                            or operation.input_fingerprint != _request_fingerprint(request)
                        ):
                            return _outcome(
                                request,
                                coordinator,
                                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                                None,
                                (
                                    "existing worker operation has a different host "
                                    "or request identity"
                                ),
                            )
                        return _existing_operation_outcome(request, operation)
                    try:
                        envelope, lease = await self._run_effect_with_lease(
                            request,
                            lease,
                            self.worker.dispatch(_snapshot_component_work_request(request)),
                        )
                    except asyncio.CancelledError:
                        self._mark_uncertain_with_status(
                            operation_key,
                            lease,
                            host_task_id=host_task_id,
                            request=request,
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001
                        durable_status, marker_detail = self._mark_uncertain_with_status(
                            operation_key,
                            lease,
                            host_task_id=host_task_id,
                            request=request,
                        )
                        return _outcome(
                            request,
                            coordinator,
                            ProgramWorkDisposition.UNCERTAIN,
                            durable_status,
                            (
                                "worker dispatch did not return trusted evidence: "
                                f"{type(exc).__name__}{marker_detail}"
                            ),
                        )
                finally:
                    semaphore.release()
                return self._record_worker_result(
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                    request=request,
                    operation_key=operation_key,
                    envelope=envelope,
                    was_uncertain=False,
                    lease=lease,
                    release_lease=False,
                )

            if operation.task_id != host_task_id or operation.operation_type != _OPERATION_TYPE:
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.NEEDS_RECONCILIATION,
                    None,
                    "worker operation belongs to a different Product Factory host task",
                )
            if operation.input_fingerprint != _request_fingerprint(request):
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.NEEDS_RECONCILIATION,
                    None,
                    "worker operation fingerprint does not match durable request",
                )
            if operation.status is IdempotencyStatus.COMPLETED:
                return _outcome(
                    request,
                    coordinator,
                    ProgramWorkDisposition.NEEDS_RECONCILIATION,
                    operation.status,
                    "completed worker operation is inconsistent with RUNNING coordinator state",
                )

            lease = await self._wait_for_effect_admission(
                semaphore=semaphore,
                request=request,
                lease=lease,
            )
            try:
                lease = self._reestablish_effect_authority(request, lease)
                claimed = self._claim_recovery_effect(
                    host_task_id=host_task_id,
                    request=request,
                    lease=lease,
                )
                if claimed is None:
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.NEEDS_RECONCILIATION,
                        IdempotencyStatus.COMPLETED,
                        "worker operation completed before recovery effect admission",
                    )
                operation = claimed
                try:
                    state, lease = await self._run_effect_with_lease(
                        request,
                        lease,
                        self.worker.inspect(request.work_id),
                    )
                except asyncio.CancelledError:
                    self._mark_uncertain_and_release_recovery_claim(
                        operation_key,
                        lease,
                        host_task_id=host_task_id,
                        request=request,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate one external inspect failure
                    durable_status, marker_detail = (
                        self._mark_uncertain_and_release_recovery_claim(
                            operation_key,
                            lease,
                            host_task_id=host_task_id,
                            request=request,
                        )
                    )
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.UNCERTAIN,
                        durable_status,
                        (
                            "worker inspection did not return trusted state: "
                            f"{type(exc).__name__}{marker_detail}"
                        ),
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
                            release_recovery_claim=True,
                            request=request,
                        )
                    except Exception:
                        coordinator.restore(before)
                        raise
                    return ProgramWorkOutcome(
                        component_id=request.component_id,
                        work_id=request.work_id,
                        disposition=ProgramWorkDisposition.BLOCKED_MISSING_WORKER_STATE,
                        state=blocked.state,
                        operation_status=self._matching_operation_status(
                            operation_key,
                            host_task_id=host_task_id,
                            request=request,
                        ),
                        detail="worker state is missing; duplicate execution is forbidden",
                    )
                try:
                    recovery_state = _snapshot_recovery_state(state)
                except (AttributeError, TypeError, ValueError):
                    durable_status, marker_detail = (
                        self._mark_uncertain_and_release_recovery_claim(
                            operation_key,
                            lease,
                            host_task_id=host_task_id,
                            request=request,
                        )
                    )
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.UNCERTAIN,
                        durable_status,
                        (
                            "worker inspection returned invalid recovery state"
                            f"{marker_detail}"
                        ),
                    )
                lease = self._reestablish_effect_authority(request, lease)
                try:
                    envelope, lease = await self._run_effect_with_lease(
                        request,
                        lease,
                        self.worker.recover(
                            _snapshot_component_work_request(request),
                            recovery_state,
                        ),
                    )
                except asyncio.CancelledError:
                    self._mark_uncertain_and_release_recovery_claim(
                        operation_key,
                        lease,
                        host_task_id=host_task_id,
                        request=request,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate one external recovery failure
                    durable_status, marker_detail = (
                        self._mark_uncertain_and_release_recovery_claim(
                            operation_key,
                            lease,
                            host_task_id=host_task_id,
                            request=request,
                        )
                    )
                    return _outcome(
                        request,
                        coordinator,
                        ProgramWorkDisposition.UNCERTAIN,
                        durable_status,
                        (
                            "worker recovery did not return trusted evidence: "
                            f"{type(exc).__name__}{marker_detail}"
                        ),
                    )
            finally:
                semaphore.release()

            return self._record_worker_result(
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
                request=request,
                operation_key=operation_key,
                envelope=envelope,
                was_uncertain=operation.status is IdempotencyStatus.UNCERTAIN,
                lease=lease,
                release_lease=False,
                recovery_claim=True,
            )
        finally:
            self._release_best_effort(lease)

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
        release_lease: bool = True,
        recovery_claim: bool = False,
    ) -> ProgramWorkOutcome:
        before = coordinator.snapshot()
        try:
            updated = coordinator.record_result(envelope)
            summary = _result_summary(updated)
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_lease(connection, lease)
                current = self._require_matching_operation(
                    connection,
                    operation_key=operation_key,
                    host_task_id=host_task_id,
                    request=request,
                )
                if current.status is IdempotencyStatus.COMPLETED:
                    raise ProductFactoryProgramError(
                        "worker operation completed before durable result finalization"
                    )
                self._checkpoint_on_connection(
                    connection,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                )
                if recovery_claim:
                    self._drop_recovery_claim(connection, operation_key, lease)
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
            if recovery_claim:
                durable_status, marker_detail = (
                    self._mark_uncertain_and_release_recovery_claim(
                        operation_key,
                        lease,
                        host_task_id=host_task_id,
                        request=request,
                    )
                )
            else:
                durable_status, marker_detail = self._mark_uncertain_with_status(
                    operation_key,
                    lease,
                    host_task_id=host_task_id,
                    request=request,
                )
            if release_lease:
                self._release_best_effort(lease)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.UNCERTAIN,
                durable_status,
                (
                    "worker evidence could not be durably reconciled: "
                    f"{type(exc).__name__}{marker_detail}"
                ),
            )

        if release_lease:
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

    def _checkpoint_running(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        requests: tuple[ComponentWorkRequest, ...],
        leases: tuple[WorkOwnershipLease, ...],
    ) -> None:
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

    def _reserve_effect(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
    ) -> tuple[IdempotencyRecord, bool]:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            return self._ledger.reserve_with_connection(
                connection,
                operation_key=_operation_key(request),
                task_id=host_task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=_request_fingerprint(request),
            )

    def _claim_recovery_effect(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
    ) -> IdempotencyRecord | None:
        operation_key = _operation_key(request)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            current = self._ledger._require_with_connection(connection, operation_key)
            if (
                current.task_id != host_task_id
                or current.operation_type != _OPERATION_TYPE
                or current.input_fingerprint != _request_fingerprint(request)
            ):
                raise ProductFactoryProgramError(
                    "worker recovery operation identity changed before effect admission"
                )
            if current.status is IdempotencyStatus.COMPLETED:
                return None
            if current.status not in {
                IdempotencyStatus.PENDING,
                IdempotencyStatus.UNCERTAIN,
            }:
                raise ProductFactoryProgramError(
                    "worker recovery operation has unsupported durable status"
                )
            row = connection.execute(
                "SELECT project_id, work_id, owner_id, fence "
                "FROM product_factory_recovery_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_factory_recovery_claims "
                    "(operation_key, project_id, work_id, owner_id, fence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        operation_key,
                        lease.project_id,
                        lease.work_id,
                        lease.owner_id,
                        lease.fence,
                    ),
                )
            else:
                same_work = (
                    row["project_id"] == lease.project_id
                    and row["work_id"] == lease.work_id
                )
                same_claim = (
                    same_work
                    and row["owner_id"] == lease.owner_id
                    and row["fence"] == lease.fence
                )
                stale_claim = (
                    same_work
                    and type(row["fence"]) is int
                    and row["fence"] < lease.fence
                )
                if same_claim:
                    pass
                elif stale_claim:
                    connection.execute(
                        "UPDATE product_factory_recovery_claims "
                        "SET owner_id = ?, fence = ? WHERE operation_key = ?",
                        (lease.owner_id, lease.fence, operation_key),
                    )
                else:
                    raise ProductFactoryProgramError(
                        "active recovery claim belongs to another authority generation"
                    )
            return current

    def _clear_stale_recovery_claim_for_reconciliation(
        self,
        connection,
        operation_key: str,
        lease: WorkOwnershipLease,
    ) -> None:
        row = connection.execute(
            "SELECT project_id, work_id, owner_id, fence "
            "FROM product_factory_recovery_claims WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:
            return
        same_work = (
            row["project_id"] == lease.project_id
            and row["work_id"] == lease.work_id
        )
        stale_claim = (
            same_work
            and type(row["fence"]) is int
            and row["fence"] < lease.fence
        )
        if not stale_claim:
            raise ProductFactoryProgramError(
                "active recovery claim blocks terminal reconciliation"
            )
        connection.execute(
            "DELETE FROM product_factory_recovery_claims WHERE operation_key = ?",
            (operation_key,),
        )

    def _drop_recovery_claim(
        self,
        connection,
        operation_key: str,
        lease: WorkOwnershipLease,
    ) -> None:
        cursor = connection.execute(
            "DELETE FROM product_factory_recovery_claims "
            "WHERE operation_key = ? AND project_id = ? AND work_id = ? "
            "AND owner_id = ? AND fence = ?",
            (
                operation_key,
                lease.project_id,
                lease.work_id,
                lease.owner_id,
                lease.fence,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductFactoryProgramError(
                "exact Product Factory recovery claim is no longer held"
            )

    def _mark_uncertain_and_release_recovery_claim(
        self,
        operation_key: str,
        lease: WorkOwnershipLease,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> tuple[IdempotencyStatus | None, str]:
        try:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_lease(connection, lease)
                current = self._require_matching_operation(
                    connection,
                    operation_key=operation_key,
                    host_task_id=host_task_id,
                    request=request,
                )
                self._drop_recovery_claim(connection, operation_key, lease)
                if current.status is IdempotencyStatus.PENDING:
                    current = self._ledger.mark_uncertain_with_connection(
                        connection,
                        operation_key,
                    )
                return current.status, ""
        except Exception as exc:  # noqa: BLE001 - never fabricate durable uncertainty
            return self._matching_operation_status(
                operation_key,
                host_task_id=host_task_id,
                request=request,
            ), f"; uncertainty marker failed: {type(exc).__name__}"

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
        release_recovery_claim: bool = False,
        request: ComponentWorkRequest,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            current = self._require_matching_operation(
                connection,
                operation_key=operation_key,
                host_task_id=host_task_id,
                request=request,
            )
            self._checkpoint_on_connection(
                connection,
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
            )
            if release_recovery_claim:
                self._drop_recovery_claim(connection, operation_key, lease)
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

    def _require_matching_operation(
        self,
        connection,
        *,
        operation_key: str,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> IdempotencyRecord:
        try:
            current = self._ledger._require_with_connection(connection, operation_key)
        except KeyError as exc:
            raise ProductFactoryProgramError(
                "worker operation disappeared before durable mutation"
            ) from exc
        if (
            current.task_id != host_task_id
            or current.operation_type != _OPERATION_TYPE
            or current.input_fingerprint != _request_fingerprint(request)
        ):
            raise ProductFactoryProgramError(
                "worker operation identity changed before durable mutation"
            )
        return current

    def _mark_uncertain_fenced(
        self,
        operation_key: str,
        lease: WorkOwnershipLease,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            current = self._require_matching_operation(
                connection,
                operation_key=operation_key,
                host_task_id=host_task_id,
                request=request,
            )
            if current.status is IdempotencyStatus.PENDING:
                self._ledger.mark_uncertain_with_connection(connection, operation_key)

    def _durable_operation_status(
        self,
        operation_key: str,
    ) -> IdempotencyStatus | None:
        try:
            current = self._ledger.get(operation_key)
        except Exception:  # noqa: BLE001 - status must not be fabricated on read failure
            return None
        return current.status if current is not None else None

    def _matching_operation_status(
        self,
        operation_key: str,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> IdempotencyStatus | None:
        try:
            current = self._ledger.get(operation_key)
        except Exception:  # noqa: BLE001 - status must not be fabricated on read failure
            return None
        if current is None:
            return None
        if (
            current.task_id != host_task_id
            or current.operation_type != _OPERATION_TYPE
            or current.input_fingerprint != _request_fingerprint(request)
        ):
            return None
        return current.status

    def _mark_uncertain_with_status(
        self,
        operation_key: str,
        lease: WorkOwnershipLease,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> tuple[IdempotencyStatus | None, str]:
        marker_detail = ""
        try:
            self._mark_uncertain_fenced(
                operation_key,
                lease,
                host_task_id=host_task_id,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001 - PENDING remains replay-blocking
            marker_detail = f"; uncertainty marker failed: {type(exc).__name__}"
        return self._matching_operation_status(
            operation_key,
            host_task_id=host_task_id,
            request=request,
        ), marker_detail

    def _acquire_if_available(
        self,
        request: ComponentWorkRequest,
    ) -> WorkOwnershipLease | None:
        try:
            return self._ownership.acquire(
                project_id=request.project_id,
                work_id=request.work_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
        except WorkOwnershipConflictError:
            return None
        except WorkOwnershipError as exc:
            raise ProductFactoryProgramError(
                f"Product Factory work ownership is unavailable for {request.work_id}: {exc}"
            ) from exc

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
        """Refresh the exact existing fence immediately before an external effect."""
        try:
            return self._ownership.renew(
                project_id=lease.project_id,
                work_id=lease.work_id,
                owner_id=lease.owner_id,
                fence=lease.fence,
                lease_seconds=self.lease_seconds,
            )
        except WorkOwnershipError as exc:
            raise ProductFactoryProgramError(
                f"stale Product Factory authority cannot start external effect for {request.work_id}: {exc}"
            ) from exc

    async def _wait_for_effect_admission(
        self,
        *,
        semaphore: asyncio.Semaphore,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
    ) -> WorkOwnershipLease:
        """Keep the exact fence alive while bounded external-effect admission waits."""

        admission = asyncio.ensure_future(semaphore.acquire())
        interval = _lease_heartbeat_interval(self.lease_seconds)
        try:
            while True:
                done, _ = await asyncio.wait((admission,), timeout=interval)
                if done:
                    admission.result()
                    return lease
                try:
                    lease = self._ownership.renew(
                        project_id=lease.project_id,
                        work_id=lease.work_id,
                        owner_id=lease.owner_id,
                        fence=lease.fence,
                        lease_seconds=self.lease_seconds,
                    )
                except WorkOwnershipError as exc:
                    raise ProductFactoryProgramError(
                        "Product Factory work ownership was lost while waiting for "
                        f"external effect admission for {request.work_id}: {exc}"
                    ) from exc
        except asyncio.CancelledError:
            await _cancel_semaphore_admission(admission, semaphore)
            raise
        except Exception:
            await _cancel_semaphore_admission(admission, semaphore)
            raise

    async def _run_effect_with_lease(
        self,
        request: ComponentWorkRequest,
        lease: WorkOwnershipLease,
        effect: Awaitable[_T],
    ) -> tuple[_T, WorkOwnershipLease]:
        """Keep the exact fence alive while one admitted external effect is in flight."""

        task = asyncio.ensure_future(effect)
        interval = _lease_heartbeat_interval(self.lease_seconds)
        try:
            while True:
                done, _ = await asyncio.wait((task,), timeout=interval)
                if done:
                    return task.result(), lease
                try:
                    lease = self._ownership.renew(
                        project_id=lease.project_id,
                        work_id=lease.work_id,
                        owner_id=lease.owner_id,
                        fence=lease.fence,
                        lease_seconds=self.lease_seconds,
                    )
                except WorkOwnershipError as exc:
                    raise ProductFactoryProgramError(
                        "Product Factory work ownership was lost during external effect "
                        f"for {request.work_id}: {exc}"
                    ) from exc
        except asyncio.CancelledError:
            await _cancel_effect_task(task)
            raise
        except Exception:
            await _cancel_effect_task(task)
            raise

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


async def _settle_work_batch(
    operations: tuple[Awaitable[ProgramWorkOutcome], ...],
) -> tuple[ProgramWorkOutcome, ...]:
    """Cancel and settle siblings before propagating any uncontained child failure."""

    tasks = tuple(asyncio.ensure_future(operation) for operation in operations)
    if not tasks:
        return ()

    pending: set[asyncio.Future[ProgramWorkOutcome]] = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            first_error: BaseException | None = None
            for task in tasks:
                if task not in done:
                    continue
                if task.cancelled():
                    if first_error is None:
                        first_error = asyncio.CancelledError()
                    continue
                error = task.exception()
                if error is not None and first_error is None:
                    first_error = error

            if first_error is not None:
                await _cancel_work_batch_tasks(tuple(pending))
                raise first_error
    except asyncio.CancelledError:
        await _cancel_work_batch_tasks(tasks)
        raise

    return tuple(task.result() for task in tasks)


async def _cancel_work_batch_tasks(
    tasks: tuple[asyncio.Future[ProgramWorkOutcome], ...],
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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


def _lease_heartbeat_interval(lease_seconds: int) -> float:
    return min(30.0, max(0.05, lease_seconds / 3.0))


async def _cancel_semaphore_admission(
    admission: asyncio.Future[bool],
    semaphore: asyncio.Semaphore,
) -> None:
    if not admission.done():
        admission.cancel()
        await asyncio.gather(admission, return_exceptions=True)
    if admission.cancelled():
        return
    with suppress(asyncio.CancelledError, Exception):
        if admission.result():
            semaphore.release()


async def _cancel_effect_task(task: asyncio.Future) -> None:
    if task.done():
        return
    task.cancel()
    done, _ = await asyncio.wait((task,), timeout=_EFFECT_CANCEL_GRACE_SECONDS)
    if done:
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        return

    # Foreign worker coroutines are not trusted to cooperate with cancellation.
    # Once Product Factory authority is lost, cleanup must not hold the host
    # indefinitely. Keep the task detached and consume any eventual exception.
    task.add_done_callback(_consume_detached_task_result)


def _consume_detached_task_result(task: asyncio.Future) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


def _canonical_recovery_text(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_RECOVERY_STATE_TEXT_UTF8_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical bounded single-line text")
    return value


def _snapshot_component_work_request(request: object) -> ComponentWorkRequest:
    if type(request) is not ComponentWorkRequest:
        raise TypeError("invalid Product Factory work request carrier")
    for value, label in (
        (request.work_id, "work_id"),
        (request.project_id, "project_id"),
        (request.component_id, "component_id"),
        (request.repository_id, "repository_id"),
        (request.goal, "goal"),
        (request.base_sha, "base_sha"),
    ):
        if type(value) is not str:
            raise TypeError(f"{label} must be exact text")
    if type(request.allowed_paths) is not tuple or any(
        type(path) is not str for path in request.allowed_paths
    ):
        raise TypeError("allowed_paths must be an exact tuple of text")
    if type(request.permission_ceiling) is not frozenset or any(
        type(permission) is not str for permission in request.permission_ceiling
    ):
        raise TypeError("permission_ceiling must be an exact frozenset of text")
    if type(request.acceptance_commands) is not tuple or any(
        type(command) is not tuple
        or any(type(argument) is not str for argument in command)
        for command in request.acceptance_commands
    ):
        raise TypeError("acceptance_commands must be exact tuples of text")
    if type(request.attempt) is not int:
        raise TypeError("attempt must be an exact integer")
    return ComponentWorkRequest(
        work_id=request.work_id,
        project_id=request.project_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        goal=request.goal,
        base_sha=request.base_sha,
        allowed_paths=tuple(request.allowed_paths),
        permission_ceiling=frozenset(request.permission_ceiling),
        acceptance_commands=tuple(
            tuple(command) for command in request.acceptance_commands
        ),
        attempt=request.attempt,
    )


def _snapshot_recovery_state(state: object) -> RecoveryState:
    if type(state) is not RecoveryState:
        raise TypeError("invalid recovery state carrier")
    phase = _canonical_recovery_text(state.phase, label="recovery phase")
    token_value = state.opaque_token
    token = (
        None
        if token_value is None
        else _canonical_recovery_text(token_value, label="recovery token")
    )
    return RecoveryState(phase, token)


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
