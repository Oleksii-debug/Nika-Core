from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)
from .gateway import ModelGateway

DEFAULT_MAX_PARALLEL_MODEL_REQUESTS = 8
MAX_PARALLEL_MODEL_REQUESTS = 64


class ParallelModelStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ParallelModelFailure:
    """Content-free failure projection for one request in a parallel batch."""

    code: ModelErrorCode
    provider_id: str | None
    retryable: bool
    failure_effect: ModelFailureEffect

    @classmethod
    def from_gateway_error(cls, error: ModelGatewayError) -> ParallelModelFailure:
        return cls(
            code=error.code,
            provider_id=error.provider_id,
            retryable=error.retryable,
            failure_effect=error.failure_effect,
        )


@dataclass(frozen=True, slots=True)
class ParallelModelOutcome:
    request_id: str
    status: ParallelModelStatus
    response: ModelResponse | None = None
    failure: ParallelModelFailure | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if self.status is ParallelModelStatus.COMPLETED:
            if self.response is None or self.failure is not None:
                raise ValueError("completed model outcome requires only a response")
            if self.response.request_id != self.request_id:
                raise ValueError("completed model outcome response identity mismatch")
        elif self.status is ParallelModelStatus.FAILED:
            if self.failure is None or self.response is not None:
                raise ValueError("failed model outcome requires only failure evidence")
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError("unsupported parallel model outcome status")


@dataclass(frozen=True, slots=True)
class ParallelModelBatchResult:
    """Stable input-order projection of one bounded fan-out/fan-in batch."""

    outcomes: tuple[ParallelModelOutcome, ...]

    def __post_init__(self) -> None:
        if not self.outcomes:
            raise ValueError("parallel model batch result must not be empty")
        request_ids = tuple(item.request_id for item in self.outcomes)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("parallel model batch result request IDs must be unique")

    @property
    def completed(self) -> tuple[ParallelModelOutcome, ...]:
        return tuple(
            item for item in self.outcomes if item.status is ParallelModelStatus.COMPLETED
        )

    @property
    def failed(self) -> tuple[ParallelModelOutcome, ...]:
        return tuple(
            item for item in self.outcomes if item.status is ParallelModelStatus.FAILED
        )

    @property
    def fully_successful(self) -> bool:
        return not self.failed


async def complete_parallel(
    gateway: ModelGateway,
    requests: Sequence[ModelRequest],
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL_MODEL_REQUESTS,
) -> ParallelModelBatchResult:
    """Run independent canonical ModelGateway requests concurrently.

    This is deliberately a thin composition layer over ``ModelGateway``. It does
    not select providers, bypass provider-specific safety locks, create another
    scheduler, or reinterpret fallback policy. Provider-specific constraints
    remain authoritative inside each registered provider.

    Outcomes are returned in the exact input order. A typed failure of one
    request is isolated as content-free failure evidence and does not erase
    successful sibling results. Cancelling the parent batch cancels every child
    task and waits for their local cancellation paths before propagating.
    """

    batch = tuple(requests)
    if not batch:
        raise ValueError("parallel model batch must contain at least one request")
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
        raise TypeError("max_parallel must be an integer")
    if not 1 <= max_parallel <= MAX_PARALLEL_MODEL_REQUESTS:
        raise ValueError(
            f"max_parallel must be between 1 and {MAX_PARALLEL_MODEL_REQUESTS}"
        )

    request_ids = tuple(request.request_id for request in batch)
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("parallel model request IDs must be unique")

    semaphore = asyncio.Semaphore(min(max_parallel, len(batch)))

    async def run_one(request: ModelRequest) -> ParallelModelOutcome:
        async with semaphore:
            try:
                response = await gateway.complete(request)
            except ModelGatewayError as error:
                return ParallelModelOutcome(
                    request_id=request.request_id,
                    status=ParallelModelStatus.FAILED,
                    failure=ParallelModelFailure.from_gateway_error(error),
                )
            if response.request_id != request.request_id:
                raise RuntimeError("parallel ModelGateway response identity mismatch")
            return ParallelModelOutcome(
                request_id=request.request_id,
                status=ParallelModelStatus.COMPLETED,
                response=response,
            )

    tasks = tuple(asyncio.create_task(run_one(request)) for request in batch)
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return ParallelModelBatchResult(tuple(outcomes))
