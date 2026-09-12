from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_program_host import (
    ProductFactoryProgramError,
    ProductFactoryProgramHost,
)
from nika_core.product_factory_work_ownership import (
    WorkOwnershipError,
    WorkOwnershipLease,
)


class _UnusedWorker:
    async def dispatch(self, request):  # pragma: no cover - forbidden by this regression
        raise AssertionError(f"unexpected dispatch for {request.work_id}")

    async def inspect(self, work_id):  # pragma: no cover - forbidden by this regression
        raise AssertionError(f"unexpected inspect for {work_id}")

    async def recover(self, request, state):  # pragma: no cover - forbidden by this regression
        raise AssertionError(f"unexpected recover for {request.work_id}: {state.phase}")


class _StaleFenceAuthority:
    def __init__(self) -> None:
        self.acquire_calls = 0

    def assert_owner(self, **kwargs) -> None:
        raise WorkOwnershipError("stale work ownership authority")

    def acquire(self, **kwargs):
        self.acquire_calls += 1
        instant = datetime(2026, 9, 11, tzinfo=UTC)
        return WorkOwnershipLease(
            project_id=kwargs["project_id"],
            work_id=kwargs["work_id"],
            owner_id=kwargs["owner_id"],
            fence=2,
            issued_at=instant,
            expires_at=instant + timedelta(minutes=5),
        )


def _request() -> ComponentWorkRequest:
    return ComponentWorkRequest(
        work_id="work-1",
        project_id="project-1",
        component_id="component-1",
        repository_id="repo-1",
        goal="implement component",
        base_sha="a" * 40,
        allowed_paths=("src/component-1",),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        acceptance_commands=(("python", "-m", "pytest", "tests/component-1"),),
    )


def test_stale_effect_authority_never_mints_replacement_fence_for_old_reservation(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    authority = _StaleFenceAuthority()
    host = ProductFactoryProgramHost(
        store,
        _UnusedWorker(),
        ownership=authority,  # type: ignore[arg-type]
        owner_id="program-host:test",
        lease_seconds=300,
    )
    request = _request()
    issued = datetime(2026, 9, 11, tzinfo=UTC)
    stale_lease = WorkOwnershipLease(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id=host.owner_id,
        fence=1,
        issued_at=issued,
        expires_at=issued + timedelta(seconds=1),
    )

    with pytest.raises(
        ProductFactoryProgramError,
        match="stale Product Factory authority cannot start external effect",
    ):
        host._reestablish_effect_authority(request, stale_lease)

    # A new fence would belong to a new authority generation. Reusing the old
    # in-memory idempotency reservation across that generation is forbidden.
    assert authority.acquire_calls == 0
