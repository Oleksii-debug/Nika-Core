from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_work_ownership import (
    ProductFactoryWorkOwnership,
    WorkOwnershipError,
    WorkOwnershipLease,
)
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION


class BehavioralText(str):
    def strip(self, *args, **kwargs):  # pragma: no cover - must not execute
        raise AssertionError("behavioral text authority executed")


class BehavioralInt(int):
    def __lt__(self, other):  # pragma: no cover - must not execute
        raise AssertionError("behavioral integer authority executed")

    def __eq__(self, other):  # pragma: no cover - must not execute
        raise AssertionError("behavioral integer authority executed")


class BehavioralDateTime(datetime):
    def utcoffset(self):  # pragma: no cover - must not execute
        raise AssertionError("behavioral datetime authority executed")


class FakeClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant

    def advance(self, **delta: int) -> None:
        self.instant += timedelta(**delta)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _service(tmp_path, *, clock: FakeClock | None = None) -> tuple[ProductFactoryWorkOwnership, FakeClock]:
    trusted_clock = clock or FakeClock(NOW)
    return ProductFactoryWorkOwnership(_store(tmp_path), clock=trusted_clock), trusted_clock


def _acquire(service: ProductFactoryWorkOwnership, *, owner: str = "worker-a", seconds: int = 60):
    return service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id=owner,
        lease_seconds=seconds,
    )


def test_table_is_created_by_canonical_ordered_migration(tmp_path) -> None:
    store = _store(tmp_path)

    with store.connection() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'product_factory_work_ownership'"
        ).fetchone()
        version = connection.execute(
            "SELECT MAX(version) FROM product_project_schema_migrations"
        ).fetchone()[0]

    assert table is not None
    assert version == PRODUCT_PROJECT_SCHEMA_VERSION


def test_public_authority_rejects_behavioral_primitive_subclasses_before_use(tmp_path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(WorkOwnershipError, match="exact canonical"):
        service.acquire(
            project_id=BehavioralText("project-1"),
            work_id="work-1",
            owner_id="worker-a",
        )

    lease = _acquire(service)
    with pytest.raises(WorkOwnershipError, match="exact positive integer"):
        service.assert_owner(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=BehavioralInt(lease.fence),
        )
    with pytest.raises(WorkOwnershipError, match="exact positive integer"):
        service.renew(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
            lease_seconds=BehavioralInt(120),
        )
    with pytest.raises(WorkOwnershipError, match="exact positive integer"):
        service.release(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=BehavioralInt(lease.fence),
        )
    with service._store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(WorkOwnershipError, match="exact positive integer"):
            service.assert_owner_in_transaction(
                connection,
                project_id=lease.project_id,
                work_id=lease.work_id,
                owner_id=lease.owner_id,
                fence=BehavioralInt(lease.fence),
            )
        connection.rollback()

    assert service.current(
        project_id=lease.project_id,
        work_id=lease.work_id,
    ) == lease


def test_clock_rejects_behavioral_datetime_subclass_before_timezone_use(tmp_path) -> None:
    clock = lambda: BehavioralDateTime(2026, 9, 11, 12, 0, tzinfo=UTC)
    service = ProductFactoryWorkOwnership(_store(tmp_path), clock=clock)

    with pytest.raises(WorkOwnershipError, match="exact datetime"):
        _acquire(service)


def test_lease_carrier_rejects_behavioral_identity_and_fence_subclasses() -> None:
    issued_at = NOW
    expires_at = NOW + timedelta(seconds=60)

    with pytest.raises(WorkOwnershipError, match="exact canonical"):
        WorkOwnershipLease(
            BehavioralText("project-1"),
            "work-1",
            "worker-a",
            1,
            issued_at,
            expires_at,
        )
    with pytest.raises(WorkOwnershipError, match="exact positive integer"):
        WorkOwnershipLease(
            "project-1",
            "work-1",
            "worker-a",
            BehavioralInt(1),
            issued_at,
            expires_at,
        )


def test_lease_carrier_rejects_equivalent_non_utc_offset_datetimes() -> None:
    offset = timezone(timedelta(hours=2))
    issued_at = NOW.astimezone(offset)
    expires_at = (NOW + timedelta(seconds=60)).astimezone(offset)

    assert issued_at == NOW
    with pytest.raises(WorkOwnershipError, match="canonical UTC"):
        WorkOwnershipLease(
            "project-1",
            "work-1",
            "worker-a",
            1,
            issued_at,
            expires_at,
        )


def test_durable_lease_rejects_equivalent_non_utc_offset_timestamps(tmp_path) -> None:
    service, _ = _service(tmp_path)
    offset = timezone(timedelta(hours=2))
    issued_at = NOW.astimezone(offset).isoformat()
    expires_at = (NOW + timedelta(seconds=60)).astimezone(offset).isoformat()
    database = tmp_path / "nika.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO product_factory_work_ownership "
            "(project_id, work_id, owner_id, fence, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project-1", "work-1", "worker-a", 7, issued_at, expires_at),
        )

    with pytest.raises(WorkOwnershipError, match="corrupt work ownership timestamp"):
        service.current(project_id="project-1", work_id="work-1")
    with pytest.raises(WorkOwnershipError, match="corrupt work ownership timestamp"):
        service.acquire(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-b",
        )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT owner_id, fence, issued_at, expires_at "
            "FROM product_factory_work_ownership WHERE project_id = ? AND work_id = ?",
            ("project-1", "work-1"),
        ).fetchone()
    assert row == ("worker-a", 7, issued_at, expires_at)


def test_trusted_clock_normalizes_non_utc_offset_before_issuing_lease(tmp_path) -> None:
    offset = timezone(timedelta(hours=2))
    service, _ = _service(tmp_path, clock=FakeClock(NOW.astimezone(offset)))

    lease = _acquire(service)

    assert lease.issued_at == NOW
    assert lease.issued_at.tzinfo is UTC
    assert lease.expires_at.tzinfo is UTC


def test_one_writer_lease_survives_restart_and_blocks_competitor(tmp_path) -> None:
    first, clock = _service(tmp_path)
    lease = _acquire(first)

    restarted = ProductFactoryWorkOwnership(_store(tmp_path), clock=clock)
    assert restarted.current(project_id="project-1", work_id="work-1") == lease
    with pytest.raises(WorkOwnershipError, match="another active owner"):
        restarted.acquire(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-b",
            lease_seconds=60,
        )


def test_expired_owner_can_be_replaced_but_stale_fence_cannot_mutate(tmp_path) -> None:
    service, clock = _service(tmp_path)
    old = _acquire(service, seconds=10)
    clock.advance(seconds=11)
    new = _acquire(service, owner="worker-b", seconds=30)

    assert new.fence > old.fence
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=old.fence,
        )
    service.assert_owner(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-b",
        fence=new.fence,
    )


def test_renew_and_release_require_exact_active_owner_and_fence(tmp_path) -> None:
    service, clock = _service(tmp_path)
    lease = _acquire(service, seconds=20)
    clock.advance(seconds=5)

    with pytest.raises(WorkOwnershipError, match="stale"):
        service.renew(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=lease.fence + 1,
            lease_seconds=20,
        )
    renewed = service.renew(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        fence=lease.fence,
        lease_seconds=20,
    )
    assert renewed.fence == lease.fence
    assert renewed.expires_at > lease.expires_at

    with pytest.raises(WorkOwnershipError, match="stale"):
        service.release(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-b",
            fence=lease.fence,
        )
    service.release(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        fence=lease.fence,
    )
    assert service.current(project_id="project-1", work_id="work-1") is None


def test_expired_owner_cannot_release_authority(tmp_path) -> None:
    service, clock = _service(tmp_path)
    lease = _acquire(service, seconds=5)
    clock.advance(seconds=6)

    with pytest.raises(WorkOwnershipError, match="stale"):
        service.release(
            project_id="project-1",
            work_id="work-1",
            owner_id=lease.owner_id,
            fence=lease.fence,
        )


def test_reacquire_after_release_never_reuses_fence_aba(tmp_path) -> None:
    service, clock = _service(tmp_path)
    first = _acquire(service, seconds=20)
    service.release(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        fence=first.fence,
    )
    clock.advance(seconds=1)
    second = _acquire(service, seconds=20)

    assert second.fence > first.fence
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=first.fence,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    (
        (None, "2026-09-11T12:01:00+00:00"),
        ("2026-09-11T12:00:00+00:00", None),
        ("not-a-time", "2026-09-11T12:01:00+00:00"),
        ("2026-09-11T12:00:00+00:00", "not-a-time"),
    ),
)
def test_acquire_fails_closed_without_overwriting_corrupt_active_row(
    tmp_path, issued_at, expires_at
) -> None:
    service, _ = _service(tmp_path)
    database = tmp_path / "nika.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO product_factory_work_ownership "
            "(project_id, work_id, owner_id, fence, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project-1", "work-1", "worker-a", 7, issued_at, expires_at),
        )

    with pytest.raises(WorkOwnershipError, match="corrupt"):
        service.acquire(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-b",
        )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT owner_id, fence, issued_at, expires_at "
            "FROM product_factory_work_ownership WHERE project_id = ? AND work_id = ?",
            ("project-1", "work-1"),
        ).fetchone()
    assert row == ("worker-a", 7, issued_at, expires_at)


def test_loaded_blob_owner_identity_fails_closed_instead_of_stringifying(tmp_path) -> None:
    service, _ = _service(tmp_path)
    database = tmp_path / "nika.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO product_factory_work_ownership "
            "(project_id, work_id, owner_id, fence, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "project-1",
                "work-1",
                sqlite3.Binary(b"worker-a"),
                7,
                "2026-09-11T12:00:00+00:00",
                "2026-09-11T12:01:00+00:00",
            ),
        )

    with pytest.raises(WorkOwnershipError, match="corrupt"):
        service.current(project_id="project-1", work_id="work-1")
    with pytest.raises(WorkOwnershipError, match="corrupt"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id="b'worker-a'",
            fence=7,
        )


def test_observation_clock_is_sampled_after_durable_read(tmp_path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(NOW)
    service = ProductFactoryWorkOwnership(store, clock=clock)
    lease = _acquire(service, seconds=1)
    original_connection = store.connection

    @contextmanager
    def delayed_connection() -> Iterator[sqlite3.Connection]:
        with original_connection() as connection:
            clock.advance(seconds=2)
            yield connection

    store.connection = delayed_connection  # type: ignore[method-assign]

    clock.instant = NOW
    assert service.current(project_id=lease.project_id, work_id=lease.work_id) is None

    clock.instant = NOW
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.assert_owner(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
        )


def test_backward_clock_observation_and_assertion_fail_closed_without_mutation(tmp_path) -> None:
    service, clock = _service(tmp_path)
    lease = _acquire(service)
    clock.instant = lease.issued_at - timedelta(seconds=1)

    with pytest.raises(WorkOwnershipError, match="precedes lease issuance"):
        service.current(project_id="project-1", work_id="work-1")
    with pytest.raises(WorkOwnershipError, match="precedes lease issuance"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id=lease.owner_id,
            fence=lease.fence,
        )

    clock.instant = lease.issued_at + timedelta(seconds=1)
    assert service.current(project_id="project-1", work_id="work-1") == lease


def test_renewal_cannot_shorten_existing_lease(tmp_path) -> None:
    service, clock = _service(tmp_path)
    lease = _acquire(service, seconds=60)
    clock.advance(seconds=1)

    with pytest.raises(WorkOwnershipError, match="extend"):
        service.renew(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=lease.fence,
            lease_seconds=30,
        )

    assert service.current(project_id="project-1", work_id="work-1") == lease


def test_lease_datetime_overflow_is_normalized_without_mutation(tmp_path) -> None:
    clock = FakeClock(datetime.max.replace(tzinfo=UTC))
    service, _ = _service(tmp_path, clock=clock)

    with pytest.raises(WorkOwnershipError, match="supported time range"):
        _acquire(service, seconds=1)

    clock.instant = NOW
    assert service.current(project_id="project-1", work_id="work-1") is None


def test_callers_cannot_supply_time_authority_per_operation(tmp_path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(TypeError, match="now"):
        service.acquire(  # type: ignore[call-arg]
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            now=NOW,
        )


class _ClockAdvancingConnection:
    def __init__(self, inner: sqlite3.Connection, clock: FakeClock) -> None:
        self._inner = inner
        self._clock = clock

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def execute(self, sql: str, parameters=()):
        cursor = self._inner.execute(sql, parameters)
        if sql.startswith(
            "SELECT owner_id, fence, issued_at, expires_at FROM product_factory_work_ownership"
        ):
            self._clock.advance(seconds=2)
        return cursor


def test_mutation_and_transaction_assert_sample_time_after_durable_row_read(tmp_path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(NOW)
    service = ProductFactoryWorkOwnership(store, clock=clock)
    lease = _acquire(service, seconds=1)
    original_connection = store.connection

    @contextmanager
    def delayed_select_connection():
        with original_connection() as connection:
            yield _ClockAdvancingConnection(connection, clock)

    store.connection = delayed_select_connection  # type: ignore[method-assign]

    clock.instant = NOW
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.renew(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
        )

    clock.instant = NOW
    replacement = service.acquire(
        project_id=lease.project_id,
        work_id=lease.work_id,
        owner_id="worker-b",
        lease_seconds=1,
    )
    assert replacement.fence == lease.fence + 1

    clock.instant = replacement.issued_at
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(WorkOwnershipError, match="stale"):
            service.assert_owner_in_transaction(
                connection,
                project_id=replacement.project_id,
                work_id=replacement.work_id,
                owner_id=replacement.owner_id,
                fence=replacement.fence,
            )


def test_mutation_clock_is_sampled_only_after_writer_serialization(tmp_path) -> None:
    store = _store(tmp_path)
    original_connection = store.connection
    active: list[sqlite3.Connection] = []

    @contextmanager
    def traced_connection() -> Iterator[sqlite3.Connection]:
        with original_connection() as connection:
            active.append(connection)
            try:
                yield connection
            finally:
                active.clear()

    store.connection = traced_connection  # type: ignore[method-assign]

    def transaction_clock() -> datetime:
        assert active and active[0].in_transaction
        return NOW

    service = ProductFactoryWorkOwnership(store, clock=transaction_clock)
    lease = _acquire(service)
    assert lease.issued_at == NOW


def test_in_transaction_assertion_requires_same_active_transaction(tmp_path) -> None:
    store = _store(tmp_path)
    service = ProductFactoryWorkOwnership(store, clock=FakeClock(NOW))
    lease = _acquire(service)

    with store.connection() as connection:
        with pytest.raises(WorkOwnershipError, match="active SQLite transaction"):
            service.assert_owner_in_transaction(
                connection,
                project_id=lease.project_id,
                work_id=lease.work_id,
                owner_id=lease.owner_id,
                fence=lease.fence,
            )
        connection.execute("BEGIN IMMEDIATE")
        service.assert_owner_in_transaction(
            connection,
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
        )


class _CommitFailureConnection:
    def __init__(self, inner: sqlite3.Connection, message: str) -> None:
        self._inner = inner
        self._message = message

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def commit(self) -> None:
        raise sqlite3.OperationalError(self._message)


def _commit_failing_store(path, message: str) -> SQLiteStore:
    store = SQLiteStore(path)

    @contextmanager
    def failing_connection():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        proxy = _CommitFailureConnection(connection, message)
        try:
            yield proxy
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    store.connection = failing_connection  # type: ignore[method-assign]
    return store


def test_commit_busy_is_normalized_and_rolls_back_acquire(tmp_path) -> None:
    path = tmp_path / "nika.db"
    _store(tmp_path)
    service = ProductFactoryWorkOwnership(
        _commit_failing_store(path, "database is locked"),
        clock=FakeClock(NOW),
    )

    with pytest.raises(WorkOwnershipError, match="busy"):
        _acquire(service)

    observer = ProductFactoryWorkOwnership(SQLiteStore(path), clock=FakeClock(NOW))
    assert observer.current(project_id="project-1", work_id="work-1") is None


def test_commit_busy_is_normalized_and_rolls_back_renew_and_release(tmp_path) -> None:
    normal, clock = _service(tmp_path)
    lease = _acquire(normal)
    clock.advance(seconds=1)
    failing = ProductFactoryWorkOwnership(
        _commit_failing_store(tmp_path / "nika.db", "database table is locked"),
        clock=clock,
    )

    with pytest.raises(WorkOwnershipError, match="busy"):
        failing.renew(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
            lease_seconds=120,
        )
    assert normal.current(project_id=lease.project_id, work_id=lease.work_id) == lease

    with pytest.raises(WorkOwnershipError, match="busy"):
        failing.release(
            project_id=lease.project_id,
            work_id=lease.work_id,
            owner_id=lease.owner_id,
            fence=lease.fence,
        )
    assert normal.current(project_id=lease.project_id, work_id=lease.work_id) == lease


def test_non_lock_commit_error_remains_raw(tmp_path) -> None:
    path = tmp_path / "nika.db"
    _store(tmp_path)
    service = ProductFactoryWorkOwnership(
        _commit_failing_store(path, "disk I/O error"),
        clock=FakeClock(NOW),
    )

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        _acquire(service)
