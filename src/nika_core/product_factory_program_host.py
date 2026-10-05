from __future__ import annotations

import asyncio
import hashlib
import json
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
    """Crash-consistent PF2 host above bounded worker dispatch, not a second runtime.

    The ordering is deliberate:

    1. coordinator state is persisted as RUNNING;
    2. the external worker operation is durably reserved;
    3. only then may the worker be called;
    4. returned evidence is reconciled and checkpointed;
    5. only after the result checkpoint is durable is the operation marked complete.

    A process loss therefore leaves enough durable state to decide whether a worker may be
    started, must be inspected/recovered, or has already produced a durable result.
    """

    store: SQLiteStore
    worker: ProductFactoryProgramWorkerPort
    idempotency: IdempotencyLedger | None = field(default=None, repr=False)
    _checkpoints: ProductFactoryCheckpointHost = field(init=False, repr=False)
    _ledger: IdempotencyLedger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._checkpoints = ProductFactoryCheckpointHost(self.store)
        self._ledger = self.idempotency or IdempotencyLedger(self.store)

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

        before_start = coordinator.snapshot()
        started = tuple(coordinator.start(request.component_id) for request in ready)
        try:
            self._save(host_task_id, binding, coordinator)
        except Exception:
            coordinator.restore(before_start)
            raise

        semaphore = asyncio.Semaphore(max_parallel)
        outcomes = await asyncio.gather(
            *(
                self._dispatch_one(
                    semaphore=semaphore,
                    host_task_id=host_task_id,
                    binding=binding,
                    coordinator=coordinator,
                    request=request,
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
            result = _result_summary(record)
            if operation.status is IdempotencyStatus.PENDING:
                self._ledger.complete(operation_key, result)
                reconciled.append(operation_key)
            elif operation.status is IdempotencyStatus.UNCERTAIN:
                self._ledger.reconcile_completed(operation_key, result)
                reconciled.append(operation_key)
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
        before = coordinator.snapshot()
        updated = coordinator.review(component_id, decision)
        try:
            self._save(host_task_id, binding, coordinator)
        except Exception:
            coordinator.restore(before)
            raise
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
        before = coordinator.snapshot()
        request = coordinator.prepare_repair(component_id, base_sha=base_sha, reason=reason)
        try:
            self._save(host_task_id, binding, coordinator)
        except Exception:
            coordinator.restore(before)
            raise
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
        before = coordinator.snapshot()
        updated = coordinator.block(component_id, reason)
        try:
            self._save(host_task_id, binding, coordinator)
        except Exception:
            coordinator.restore(before)
            raise
        return updated

    async def _dispatch_one(
        self,
        *,
        semaphore: asyncio.Semaphore,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        request: ComponentWorkRequest,
    ) -> ProgramWorkOutcome:
        async with semaphore:
            operation_key = _operation_key(request)
            operation, created = self._ledger.reserve_once(
                operation_key=operation_key,
                task_id=host_task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=_request_fingerprint(request),
            )
            if not created:
                return _existing_operation_outcome(request, operation)
            try:
                envelope = await self.worker.dispatch(request)
            except asyncio.CancelledError:
                self._mark_uncertain(operation_key)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external worker failure
                self._mark_uncertain(operation_key)
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
        operation = self._ledger.get(operation_key)

        if operation is None:
            return await self._dispatch_one(
                semaphore=semaphore,
                host_task_id=host_task_id,
                binding=binding,
                coordinator=coordinator,
                request=request,
            )

        if operation.task_id != host_task_id or operation.operation_type != _OPERATION_TYPE:
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                operation.status,
                "worker operation belongs to a different Product Factory host task",
            )
        if operation.input_fingerprint != _request_fingerprint(request):
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.NEEDS_RECONCILIATION,
                operation.status,
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

        async with semaphore:
            state = await self.worker.inspect(request.work_id)
            if state is None:
                self._mark_uncertain(operation_key)
                before = coordinator.snapshot()
                blocked = coordinator.block(
                    request.component_id,
                    "worker recovery state is unavailable; explicit reconciliation required",
                )
                try:
                    self._save(host_task_id, binding, coordinator)
                except Exception:
                    coordinator.restore(before)
                    raise
                return ProgramWorkOutcome(
                    component_id=request.component_id,
                    work_id=request.work_id,
                    disposition=ProgramWorkDisposition.BLOCKED_MISSING_WORKER_STATE,
                    state=blocked.state,
                    operation_status=IdempotencyStatus.UNCERTAIN,
                    detail="worker state is missing; duplicate execution is forbidden",
                )
            try:
                envelope = await self.worker.recover(request, state)
            except asyncio.CancelledError:
                self._mark_uncertain(operation_key)
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one external recovery failure
                self._mark_uncertain(operation_key)
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
    ) -> ProgramWorkOutcome:
        before = coordinator.snapshot()
        try:
            updated = coordinator.record_result(envelope)
            self._save(host_task_id, binding, coordinator)
        except Exception as exc:  # noqa: BLE001 - preserve uncertain external-side-effect state
            coordinator.restore(before)
            self._mark_uncertain(operation_key)
            return _outcome(
                request,
                coordinator,
                ProgramWorkDisposition.UNCERTAIN,
                IdempotencyStatus.UNCERTAIN,
                f"worker evidence could not be durably reconciled: {type(exc).__name__}",
            )

        summary = _result_summary(updated)
        try:
            if was_uncertain:
                self._ledger.reconcile_completed(operation_key, summary)
            else:
                self._ledger.complete(operation_key, summary)
        except Exception as exc:  # noqa: BLE001 - durable result forbids worker replay
            return ProgramWorkOutcome(
                component_id=request.component_id,
                work_id=request.work_id,
                disposition=ProgramWorkDisposition.NEEDS_RECONCILIATION,
                state=updated.state,
                operation_status=self._ledger.require(operation_key).status,
                detail=(
                    "result is durable but operation ledger needs reconciliation: "
                    f"{type(exc).__name__}"
                ),
            )

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
            detail="worker evidence is durable and awaits the next Product Factory decision",
        )

    def _save(
        self,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
    ) -> None:
        self._checkpoints.save(
            host_task_id=host_task_id,
            checkpoint=binding.checkpoint(coordinator),
        )

    def _mark_uncertain(self, operation_key: str) -> None:
        operation = self._ledger.require(operation_key)
        if operation.status is IdempotencyStatus.PENDING:
            self._ledger.mark_uncertain(operation_key)


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
