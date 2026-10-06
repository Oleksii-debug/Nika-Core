from __future__ import annotations

from collections.abc import Callable

from .contracts import CodingJob, CodingResult, CodingWorkerPort, RecoveryState


class DeterministicCodingWorker(CodingWorkerPort):
    """Test-only deterministic worker. It never executes subprocesses or network requests."""

    def __init__(self, responder: Callable[[CodingJob], CodingResult]) -> None:
        self._responder = responder
        self.executions: list[str] = []
        self.cancelled: set[str] = set()
        self.recovery: dict[str, RecoveryState] = {}

    async def execute(self, job: CodingJob) -> CodingResult:
        self.executions.append(job.job_id)
        result = self._responder(job)
        if result.recovery_state is not None:
            self.recovery[job.job_id] = result.recovery_state
        return result

    async def cancel(self, job_id: str) -> None:
        self.cancelled.add(job_id)

    async def inspect(self, job_id: str) -> RecoveryState | None:
        return self.recovery.get(job_id)

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        self.recovery[job.job_id] = state
        return await self.execute(job)
