from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_work_ownership import (
    ProductFactoryWorkOwnership,
    WorkOwnershipError,
)


def _service(tmp_path) -> ProductFactoryWorkOwnership:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return ProductFactoryWorkOwnership(store)


def test_table_is_created_by_canonical_ordered_migration(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with store.connection() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'product_factory_work_ownership'"
        ).fetchone()
        version = connection.execute(
            "SELECT MAX(version) FROM product_project_schema_migrations"
        ).fetchone()[0]

    assert table is not None
    assert version == 3


def test_one_writer_lease_survives_restart_and_blocks_competitor(tmp_path) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    first = _service(tmp_path)
    lease = first.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=now,
        lease_seconds=60,
    )

    restarted = _service(tmp_path)
    assert restarted.current(project_id="project-1", work_id="work-1", now=now) == lease
    with pytest.raises(WorkOwnershipError, match="owned"):
        restarted.acquire(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-b",
            now=now + timedelta(seconds=1),
            lease_seconds=60,
        )


def test_expired_owner_can_be_replaced_but_stale_fence_cannot_mutate(tmp_path) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    service = _service(tmp_path)
    old = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=now,
        lease_seconds=10,
    )
    new = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-b",
        now=now + timedelta(seconds=11),
        lease_seconds=30,
    )

    assert new.fence > old.fence
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=old.fence,
            now=now + timedelta(seconds=12),
        )
    service.assert_owner(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-b",
        fence=new.fence,
        now=now + timedelta(seconds=12),
    )


def test_renew_and_release_require_exact_owner_and_fence(tmp_path) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    service = _service(tmp_path)
    lease = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=now,
        lease_seconds=20,
    )

    with pytest.raises(WorkOwnershipError, match="stale"):
        service.renew(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=lease.fence + 1,
            now=now + timedelta(seconds=5),
            lease_seconds=20,
        )
    renewed = service.renew(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        fence=lease.fence,
        now=now + timedelta(seconds=5),
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
    assert service.current(
        project_id="project-1",
        work_id="work-1",
        now=now + timedelta(seconds=6),
    ) is None


def test_reacquire_after_release_never_reuses_fence_aba(tmp_path) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    service = _service(tmp_path)
    first = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=now,
        lease_seconds=20,
    )
    service.release(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        fence=first.fence,
    )
    second = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=now + timedelta(seconds=1),
        lease_seconds=20,
    )

    assert second.fence > first.fence
    with pytest.raises(WorkOwnershipError, match="stale"):
        service.assert_owner(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=first.fence,
            now=now + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    (
        (None, "2026-09-08T12:01:00+00:00"),
        ("2026-09-08T12:00:00+00:00", None),
        ("not-a-time", "2026-09-08T12:01:00+00:00"),
        ("2026-09-08T12:00:00+00:00", "not-a-time"),
    ),
)
def test_acquire_fails_closed_without_overwriting_corrupt_active_row(
    tmp_path, issued_at, expires_at
) -> None:
    service = _service(tmp_path)
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
            now=datetime(2026, 9, 8, 12, 2, tzinfo=UTC),
        )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT owner_id, fence, issued_at, expires_at "
            "FROM product_factory_work_ownership WHERE project_id = ? AND work_id = ?",
            ("project-1", "work-1"),
        ).fetchone()
    assert row == ("worker-a", 7, issued_at, expires_at)


def test_backward_clock_renewal_fails_without_corrupting_restart_state(tmp_path) -> None:
    issued = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    service = _service(tmp_path)
    lease = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=issued,
        lease_seconds=60,
    )

    with pytest.raises(WorkOwnershipError, match="precedes lease issuance"):
        service.renew(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=lease.fence,
            now=issued - timedelta(minutes=1),
            lease_seconds=30,
        )

    restarted = _service(tmp_path)
    assert restarted.current(
        project_id="project-1",
        work_id="work-1",
        now=issued + timedelta(seconds=1),
    ) == lease


def test_renewal_cannot_shorten_existing_lease(tmp_path) -> None:
    issued = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    service = _service(tmp_path)
    lease = service.acquire(
        project_id="project-1",
        work_id="work-1",
        owner_id="worker-a",
        now=issued,
        lease_seconds=60,
    )

    with pytest.raises(WorkOwnershipError, match="extend"):
        service.renew(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            fence=lease.fence,
            now=issued + timedelta(seconds=1),
            lease_seconds=30,
        )

    assert service.current(
        project_id="project-1",
        work_id="work-1",
        now=issued + timedelta(seconds=2),
    ) == lease


def test_lease_datetime_overflow_is_normalized_without_mutation(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(WorkOwnershipError, match="supported time range"):
        service.acquire(
            project_id="project-1",
            work_id="work-1",
            owner_id="worker-a",
            now=datetime.max.replace(tzinfo=UTC),
            lease_seconds=1,
        )

    assert service.current(
        project_id="project-1",
        work_id="work-1",
        now=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    ) is None
