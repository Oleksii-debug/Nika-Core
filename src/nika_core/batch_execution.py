from __future__ import annotations

from asyncio import gather as _gather
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from nika_core.resources import ResourceManager


class BatchStopReason(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    RESOURCE_BLOCKED = "resource_blocked"


class BatchTargetState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_STARTED = "not_started"


@dataclass(frozen=True, slots=True)
class BatchControlSnapshot:
    paused: bool = False
    cancelled: bool = False
    deadline_reached: bool = False


@dataclass(frozen=True, slots=True)
class BatchTargetResult:
    index: int
    target_id: str
    state: BatchTargetState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BatchExecutionReport:
    stop_reason: BatchStopReason
    results: tuple[BatchTargetResult, ...]
    peak_in_flight: int
    resource_reason: str | None = None

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def completed_count(self) -> int:
        return sum(result.state is BatchTargetState.COMPLETED for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.state is BatchTargetState.FAILED for result in self.results)

    @property
    def not_started_count(self) -> int:
        return sum(result.state is BatchTargetState.NOT_STARTED for result in self.results)


class BoundedBatchExecutor[TargetT]:
    """Execute declared targets in bounded batches under Nika's ResourceManager.

    Only the current batch is materialized as async work. Pause, cancel and deadline
    snapshots are checked before each next batch, so already-started work can settle while
    no later target is launched. Per-target exceptions are isolated and reported without
    exposing exception messages.
    """

    def __init__(
        self,
        *,
        resources: ResourceManager,
        resource_scope: str,
        resource_owner_id: str,
        run_id: str,
        batch_size: int,
        control: Callable[[], BatchControlSnapshot] | None = None,
    ) -> None:
        if not resource_scope.strip() or not resource_owner_id.strip():
            raise ValueError("resource scope and owner_id must not be empty")
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        self._resources = resources
        self._resource_scope = resource_scope
        self._resource_owner_id = resource_owner_id
        self._run_id = run_id
        self._batch_size = batch_size
        self._control = control or BatchControlSnapshot

    async def run(
        self,
        targets: Sequence[TargetT],
        *,
        identify: Callable[[TargetT], str],
        execute: Callable[[TargetT], Awaitable[None]],
    ) -> BatchExecutionReport:
        declared = tuple(targets)
        target_ids = tuple(self._target_id(identify(target)) for target in declared)
        results: list[BatchTargetResult | None] = [None] * len(declared)
        next_index = 0
        peak_in_flight = 0
        resource_reason: str | None = None
        stop_reason: BatchStopReason | None = None

        if not declared:
            return BatchExecutionReport(BatchStopReason.COMPLETED, (), 0)

        budget = self._resources.get_budget(
            scope=self._resource_scope,
            owner_id=self._resource_owner_id,
        )
        effective_batch_size = min(self._batch_size, budget.max_concurrent)
        if effective_batch_size <= 0:
            raise RuntimeError("resource manager returned a non-positive concurrency budget")

        while next_index < len(declared):
            stop_reason = self._control_stop_reason(self._control())
            if stop_reason is not None:
                break

            end_index = min(next_index + effective_batch_size, len(declared))
            admitted: list[tuple[int, TargetT, str]] = []
            for index in range(next_index, end_index):
                request_id = self._request_id(index)
                decision = self._resources.request(
                    scope=self._resource_scope,
                    owner_id=self._resource_owner_id,
                    request_id=request_id,
                )
                if not decision.granted:
                    self._resources.cancel_waiting(
                        scope=self._resource_scope,
                        owner_id=self._resource_owner_id,
                        request_id=request_id,
                    )
                    for _, _, admitted_request_id in admitted:
                        self._resources.release(
                            scope=self._resource_scope,
                            owner_id=self._resource_owner_id,
                            request_id=admitted_request_id,
                        )
                    resource_reason = decision.reason
                    stop_reason = BatchStopReason.RESOURCE_BLOCKED
                    admitted.clear()
                    break
                admitted.append((index, declared[index], request_id))

            if stop_reason is BatchStopReason.RESOURCE_BLOCKED:
                break

            peak_in_flight = max(peak_in_flight, len(admitted))
            batch_results = await _gather(
                *(
                    self._execute_one(index, target_ids[index], target, request_id, execute)
                    for index, target, request_id in admitted
                )
            )
            for result in batch_results:
                results[result.index] = result
            next_index = end_index

        final_stop_reason = stop_reason or BatchStopReason.COMPLETED
        not_started_reason = (
            f"resource_blocked:{resource_reason}"
            if final_stop_reason is BatchStopReason.RESOURCE_BLOCKED and resource_reason
            else final_stop_reason.value
        )
        final_results = tuple(
            result
            if result is not None
            else BatchTargetResult(
                index=index,
                target_id=target_ids[index],
                state=BatchTargetState.NOT_STARTED,
                reason=not_started_reason,
            )
            for index, result in enumerate(results)
        )
        return BatchExecutionReport(
            stop_reason=final_stop_reason,
            results=final_results,
            peak_in_flight=peak_in_flight,
            resource_reason=resource_reason,
        )

    async def _execute_one(
        self,
        index: int,
        target_id: str,
        target: TargetT,
        request_id: str,
        execute: Callable[[TargetT], Awaitable[None]],
    ) -> BatchTargetResult:
        try:
            await execute(target)
        except Exception as exc:  # noqa: BLE001 - isolate one target from unrelated work.
            return BatchTargetResult(
                index=index,
                target_id=target_id,
                state=BatchTargetState.FAILED,
                reason=type(exc).__name__,
            )
        finally:
            self._resources.release(
                scope=self._resource_scope,
                owner_id=self._resource_owner_id,
                request_id=request_id,
            )
        return BatchTargetResult(
            index=index,
            target_id=target_id,
            state=BatchTargetState.COMPLETED,
        )

    def _request_id(self, index: int) -> str:
        return f"batch:{self._run_id}:{index}"

    @staticmethod
    def _target_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("target identity must be a non-empty string")
        return value

    @staticmethod
    def _control_stop_reason(snapshot: BatchControlSnapshot) -> BatchStopReason | None:
        if snapshot.cancelled:
            return BatchStopReason.CANCELLED
        if snapshot.paused:
            return BatchStopReason.PAUSED
        if snapshot.deadline_reached:
            return BatchStopReason.DEADLINE
        return None
