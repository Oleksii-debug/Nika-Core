from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

from nika_core.data.sqlite import SQLiteStore
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk


def test_identical_concurrent_standing_permission_grants_converge(tmp_path) -> None:
    """A duplicated restart/retry must not leak a raw SQLite uniqueness failure."""
    store = SQLiteStore(tmp_path / "standing-authority.db")
    store.initialize()
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    now = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
    scope = StandingPermissionScope(
        subject_id="agent-parent",
        context=PermissionContext(
            user_id="user-1",
            project_id="project-1",
            task_id="task-1",
        ),
        action_class="browser.inspect",
        targets=("target:listing-1",),
        sites=("example.test",),
        resources=("resource:price",),
        risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
        granted_at=now,
        expires_at=now + timedelta(hours=1),
    )

    both_observed_absent = Barrier(2)
    counter_lock = Lock()
    absent_reads = 0
    original_get = permissions._get

    def synchronized_get(conn, permission_id):
        nonlocal absent_reads
        record = original_get(conn, permission_id)
        should_wait = False
        if permission_id == "permission-1" and record is None:
            with counter_lock:
                if absent_reads < 2:
                    absent_reads += 1
                    should_wait = True
        if should_wait:
            both_observed_absent.wait(timeout=5)
        return record

    permissions._get = synchronized_get  # type: ignore[method-assign]

    def grant():
        return permissions.grant(permission_id="permission-1", scope=scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(future.result(timeout=5) for future in (pool.submit(grant), pool.submit(grant)))

    assert results[0].permission_id == results[1].permission_id == "permission-1"
    assert results[0].scope_fingerprint == results[1].scope_fingerprint

