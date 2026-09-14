from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import islice
from typing import Protocol

from .contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)
from .gateway import ModelGateway

DEFAULT_MAX_PARALLEL_MODEL_REQUESTS = 8
MAX_PARALLEL_MODEL_REQUESTS = 256
MAX_PARALLEL_MODEL_BATCH_REQUESTS = 256
MAX_PARALLEL_MODEL_PROVIDER_LIMITS = 256


class ParallelModelExecutionScopeDenied(RuntimeError):
    """Trusted request-specific execution scope could not be bound pre-effect."""


class ParallelModelExecutionScopePort(Protocol):
    """Trusted per-request execution context entered inside each child task.

    The port intentionally carries only request identity, never provider/model
    payload authority. A security/runtime adapter may use this seam to bind
    already-authorized host context to the exact asyncio child task immediately
    before ``ModelGateway.complete``. The gateway remains the effect authority.

    A request-specific, known pre-effect denial may raise
    ``ParallelModelExecutionScopeDenied`` from ``scope_for`` or context entry so
    only that child is projected as INVALID_REQUEST/NO_EFFECT. Structural adapter
    errors and context-exit failures are not normalized because an exit failure
    may occur after provider effect and therefore cannot truthfully claim NO_EFFECT.
    """

    def scope_for(self, *, request_id: str) -> AbstractContextManager[None]: ...


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
    provider_limits: Mapping[str, int] | None = None,
    execution_scopes: ParallelModelExecutionScopePort | None = None,
) -> ParallelModelBatchResult:
    """Run independent canonical ModelGateway requests concurrently.

    This is deliberately a thin composition layer over ``ModelGateway``. It does
    not select providers, bypass provider-specific safety locks, create another
    scheduler, or reinterpret fallback policy. Provider-specific constraints
    remain authoritative inside each registered provider.

    ``max_parallel`` bounds active execution. The admitted fan-out cardinality is
    independently capped by ``MAX_PARALLEL_MODEL_BATCH_REQUESTS`` before the
    sequence is copied or child tasks are created. The bounded copy below also
    fails closed if a mutable or nonconforming Sequence changes/understates its
    cardinality during admission. The default remains intentionally conservative;
    callers must explicitly opt into larger concurrency after their resource and
    provider policy allows it.

    Each child receives one monotonic request budget snapshotted before any child
    task is created. Event-loop scheduling delay, provider/global semaphore wait
    and inference therefore consume the same ``ModelRequest.timeout_seconds``
    budget. Expiry before provider entry is a typed TIMEOUT with positive
    NO_EFFECT truth and no provider call.

    Optional ``provider_limits`` add route-specific admission for requests pinned
    to explicit ``provider_id`` values. The mapping is itself bounded before it is
    copied so admission metadata cannot become an unbounded allocation surface.
    A request waits for its provider slot *before* it takes a global slot,
    preventing a saturated or slow provider from occupying every global slot
    while unrelated providers/local routes are ready to run.

    Provider-limited batches deliberately reject hidden ModelGateway fallbacks:
    an inner fallback attempt could switch to a provider whose semaphore this
    layer did not acquire. Callers that need provider ceilings therefore fan out
    explicit provider routes. Ordinary single-request ModelGateway fallback
    remains available when provider-specific admission is not requested.

    ``execution_scopes`` is an optional trusted composition seam, not an
    authorization mechanism. Its per-request context is entered only after
    admission and *inside the exact child asyncio task* immediately around the
    canonical gateway call. This lets task-bound security authorities bind each
    child explicitly without allowing a parent task's authority to be inherited.
    Omitting the scope never weakens ModelGateway authorization; a gateway that
    requires current host authority still fails closed.

    Outcomes are returned in the exact input order. A typed failure of one
    request is isolated as content-free failure evidence and does not erase
    successful sibling results. A child-local/spurious cancellation is likewise
    isolated as CANCELLED/UNKNOWN; only cancellation of the parent batch itself
    propagates through every child. Cancelling the parent batch cancels every
    child task and waits for their local cancellation paths before propagating.
    This is local coroutine cancellation only; callers must consult canonical
    route capabilities before claiming underlying provider inference was hard-
    cancelled.
    """

    _validate_limit(max_parallel, name="max_parallel")
    expected_batch_size = len(requests)
    if expected_batch_size < 1:
        raise ValueError("parallel model batch must contain at least one request")
    if expected_batch_size > MAX_PARALLEL_MODEL_BATCH_REQUESTS:
        raise ValueError(
            "parallel model batch must contain at most "
            f"{MAX_PARALLEL_MODEL_BATCH_REQUESTS} requests"
        )

    # Sequence.__len__ lets ordinary oversized input fail before any copy. islice
    # keeps even a mutable/hostile Sequence bounded if iteration disagrees with
    # the admitted length, so no caller can turn one batch into unbounded task
    # allocation between admission and materialization.
    batch = tuple(islice(requests, MAX_PARALLEL_MODEL_BATCH_REQUESTS + 1))
    if len(batch) != expected_batch_size:
        raise ValueError("parallel model batch changed during admission")
    if len(batch) > MAX_PARALLEL_MODEL_BATCH_REQUESTS:
        raise ValueError(
            "parallel model batch must contain at most "
            f"{MAX_PARALLEL_MODEL_BATCH_REQUESTS} requests"
        )

    request_ids = tuple(request.request_id for request in batch)
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("parallel model request IDs must be unique")

    limits = _validated_provider_limits(gateway, provider_limits)
    _validate_provider_limited_routes(batch, limits)
    global_semaphore = asyncio.Semaphore(min(max_parallel, len(batch)))
    provider_semaphores = {
        provider_id: asyncio.Semaphore(limit) for provider_id, limit in limits.items()
    }

    # Deadline identity is fixed before create_task(). Otherwise a child delayed
    # by event-loop pressure would receive a fresh timeout budget when it finally
    # begins executing, which violates the end-to-end request deadline contract.
    loop = asyncio.get_running_loop()
    accepted_at = loop.time()
    deadlines = tuple(accepted_at + request.timeout_seconds for request in batch)
    batch_task = asyncio.current_task()

    async def execute(request: ModelRequest) -> ParallelModelOutcome:
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

    async def run_one(request: ModelRequest, deadline: float) -> ParallelModelOutcome:
        provider_semaphore = (
            provider_semaphores.get(request.provider_id)
            if request.provider_id is not None
            else None
        )
        provider_acquired = False
        global_acquired = False
        try:
            # Provider admission comes first. Otherwise multiple requests queued on
            # one provider could consume every global slot while merely waiting for
            # that provider, head-of-line blocking an independent route.
            if provider_semaphore is not None:
                provider_acquired = await _acquire_before_deadline(
                    provider_semaphore, deadline
                )
                if not provider_acquired:
                    return _admission_timeout_outcome(request)

            global_acquired = await _acquire_before_deadline(global_semaphore, deadline)
            if not global_acquired:
                return _admission_timeout_outcome(request)

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return _admission_timeout_outcome(request)
            admitted_request = replace(request, timeout_seconds=remaining)
            if execution_scopes is None:
                return await execute(admitted_request)

            scope_stack = ExitStack()
            try:
                scope = execution_scopes.scope_for(request_id=request.request_id)
                scope_stack.enter_context(scope)
            except ParallelModelExecutionScopeDenied:
                scope_stack.close()
                return _execution_scope_denied_outcome(request)
            with scope_stack:
                return await execute(admitted_request)
        except asyncio.CancelledError:
            # A provider/callback can raise or self-request CancelledError inside
            # one child. That must not grant one route authority to cancel
            # unrelated siblings. Only an actual cancellation request on the
            # parent batch is batch-wide. The isolated child effect is UNKNOWN:
            # local coroutine cancellation does not prove the provider stopped.
            if batch_task is not None and batch_task.cancelling() > 0:
                raise
            return _child_cancelled_outcome(request)
        finally:
            if global_acquired:
                global_semaphore.release()
            if provider_acquired and provider_semaphore is not None:
                provider_semaphore.release()

    tasks = tuple(
        asyncio.create_task(run_one(request, deadline))
        for request, deadline in zip(batch, deadlines, strict=True)
    )
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return ParallelModelBatchResult(tuple(outcomes))


async def _acquire_before_deadline(
    semaphore: asyncio.Semaphore,
    deadline: float,
) -> bool:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return False
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=remaining)
    except TimeoutError:
        return False
    return True


def _admission_timeout_outcome(request: ModelRequest) -> ParallelModelOutcome:
    return ParallelModelOutcome(
        request_id=request.request_id,
        status=ParallelModelStatus.FAILED,
        failure=ParallelModelFailure(
            code=ModelErrorCode.TIMEOUT,
            provider_id=request.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        ),
    )


def _execution_scope_denied_outcome(request: ModelRequest) -> ParallelModelOutcome:
    return ParallelModelOutcome(
        request_id=request.request_id,
        status=ParallelModelStatus.FAILED,
        failure=ParallelModelFailure(
            code=ModelErrorCode.INVALID_REQUEST,
            provider_id=request.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        ),
    )


def _child_cancelled_outcome(request: ModelRequest) -> ParallelModelOutcome:
    return ParallelModelOutcome(
        request_id=request.request_id,
        status=ParallelModelStatus.FAILED,
        failure=ParallelModelFailure(
            code=ModelErrorCode.CANCELLED,
            provider_id=request.provider_id,
            retryable=False,
            failure_effect=ModelFailureEffect.UNKNOWN,
        ),
    )


def _validated_provider_limits(
    gateway: ModelGateway,
    provider_limits: Mapping[str, int] | None,
) -> dict[str, int]:
    if provider_limits is None:
        return {}
    if not isinstance(provider_limits, Mapping):
        raise TypeError("provider_limits must be a mapping")

    expected_limit_count = len(provider_limits)
    if expected_limit_count > MAX_PARALLEL_MODEL_PROVIDER_LIMITS:
        raise ValueError(
            "provider_limits must contain at most "
            f"{MAX_PARALLEL_MODEL_PROVIDER_LIMITS} entries"
        )
    limit_items = tuple(
        islice(provider_limits.items(), MAX_PARALLEL_MODEL_PROVIDER_LIMITS + 1)
    )
    if len(limit_items) != expected_limit_count:
        raise ValueError("provider_limits changed during admission")
    if len(limit_items) > MAX_PARALLEL_MODEL_PROVIDER_LIMITS:
        raise ValueError(
            "provider_limits must contain at most "
            f"{MAX_PARALLEL_MODEL_PROVIDER_LIMITS} entries"
        )

    registered = frozenset(gateway.providers())
    validated: dict[str, int] = {}
    for provider_id, limit in limit_items:
        if type(provider_id) is not str:
            raise TypeError("provider limit ID must be text")
        if not provider_id or provider_id != provider_id.strip():
            raise ValueError("provider limit ID must be non-empty canonical text")
        if provider_id in validated:
            raise ValueError("provider_limits contains duplicate provider IDs")
        if provider_id not in registered:
            raise ValueError(f"provider limit references an unknown provider: {provider_id}")
        _validate_limit(limit, name=f"provider limit for {provider_id}")
        validated[provider_id] = limit
    return validated


def _validate_provider_limited_routes(
    requests: tuple[ModelRequest, ...],
    provider_limits: Mapping[str, int],
) -> None:
    if not provider_limits:
        return
    for request in requests:
        if request.fallback_provider_ids:
            raise ValueError(
                "provider-limited parallel requests must use explicit routes without fallback"
            )
        if request.provider_id is None:
            raise ValueError(
                "provider-limited parallel requests require an explicit provider_id"
            )


def _validate_limit(value: int, *, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= MAX_PARALLEL_MODEL_REQUESTS:
        raise ValueError(
            f"{name} must be between 1 and {MAX_PARALLEL_MODEL_REQUESTS}"
        )
