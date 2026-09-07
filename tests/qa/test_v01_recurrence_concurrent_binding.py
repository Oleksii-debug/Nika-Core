from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler.contracts import ScheduledJob
from nika_core.scheduler.recurrence import DurableRecurrenceService, RecurrenceState
from nika_core.scheduler.store import ScheduledJobStore


class _BarrierJobReader:
    """Force both creators to observe the same pre-bind absence."""

    def __init__(self, inner: ScheduledJobStore, barrier: Barrier) -> None:
        self._inner = inner
        self._barrier = barrier

    def get(self, job_id: str) -> ScheduledJob | None:
        job = self._inner.get(job_id)
        self._barrier.wait(timeout=5)
        return job


class _SerialPersistingScheduler:
    """Serialize writes after the deliberately concurrent read window."""

    def __init__(self, jobs: ScheduledJobStore, lock: Lock) -> None:
        self._jobs = jobs
        self._lock = lock

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        with self._lock:
            self._jobs.upsert(job)

    def remove(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.delete(job_id)

    def pause(self, job_id: str) -> None:
        with self._lock:
            if not self._jobs.set_enabled(job_id, False):
                raise KeyError(job_id)

    def resume(self, job_id: str) -> None:
        with self._lock:
            if not self._jobs.set_enabled(job_id, True):
                raise KeyError(job_id)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "recurrence-concurrent-binding.db")
    store.initialize()
    return store


def _service(
    *,
    reader: _BarrierJobReader,
    scheduler: _SerialPersistingScheduler,
) -> DurableRecurrenceService:
    def resolve(_action_id: str):
        def handler(_invocation: object) -> None:
            return None

        return handler

    return DurableRecurrenceService(
        jobs=reader,  # type: ignore[arg-type] -- deterministic QA read seam only.
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )


def test_concurrent_conflicting_create_cannot_rebind_one_recurrence_id(
    tmp_path: Path,
) -> None:
    """Exactly one immutable recurrence definition may acquire durable ownership."""

    store = _store(tmp_path)
    durable_jobs = ScheduledJobStore(store)
    read_barrier = Barrier(2)
    write_lock = Lock()

    first = _service(
        reader=_BarrierJobReader(durable_jobs, read_barrier),
        scheduler=_SerialPersistingScheduler(durable_jobs, write_lock),
    )
    second = _service(
        reader=_BarrierJobReader(durable_jobs, read_barrier),
        scheduler=_SerialPersistingScheduler(durable_jobs, write_lock),
    )
    start = datetime(2030, 1, 1, 12, 5, tzinfo=UTC)

    def create(
        service: DurableRecurrenceService,
        *,
        interval_seconds: int,
        scope: str,
    ) -> RecurrenceState:
        return service.create(
            recurrence_id="shared-monitor",
            action_id="monitor.check",
            interval_seconds=interval_seconds,
            start_at=start,
            payload={"scope": scope},
        )

    outcomes: list[RecurrenceState | Exception] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(create, first, interval_seconds=60, scope="alpha"),
            executor.submit(create, second, interval_seconds=300, scope="beta"),
        )
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except Exception as exc:  # noqa: BLE001 - the losing create must fail closed.
                outcomes.append(exc)

    successes = [item for item in outcomes if isinstance(item, RecurrenceState)]
    failures = [item for item in outcomes if isinstance(item, Exception)]

    assert len(successes) == 1, (
        "same recurrence_id acquired two conflicting definitions concurrently; "
        "the durable identity can be silently rebound"
    )
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "different recurrence" in str(failures[0])

    durable = DurableRecurrenceService(
        jobs=durable_jobs,
        scheduler=_SerialPersistingScheduler(durable_jobs, write_lock),
        handler_resolver=lambda _action_id: lambda _invocation: None,
        clock=lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    ).get("shared-monitor")
    assert durable is not None
    assert durable == successes[0]
