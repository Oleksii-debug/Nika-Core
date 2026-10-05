from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_program import (
    build_product_factory_coding_program_host,
)
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_work_ownership import ProductFactoryWorkOwnership
from nika_core.runtime.idempotency import IdempotencyLedger


class _UnusedWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request}")

    async def inspect(self, work_id):
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request}, {state}")


class _UnusedContexts:
    async def context_for(self, request):
        raise AssertionError(f"unexpected context: {request}")


class _UnusedEvidence:
    async def collect(self, request, job, result):
        raise AssertionError(f"unexpected evidence: {request}, {job}, {result}")


@pytest.mark.parametrize("different_path", [False, True])
def test_program_host_rejects_cross_store_ledger_before_worker_effect(
    tmp_path: Path, different_path: bool
) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    other_path = tmp_path / ("other.db" if different_path else "factory.db")
    other = SQLiteStore(other_path)
    other.initialize()
    ledger = IdempotencyLedger(other)

    with pytest.raises(ValueError, match="must use the host SQLiteStore instance"):
        ProductFactoryProgramHost(
            store=store,
            worker=_UnusedWorker(),
            idempotency=ledger,
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0] == 0
    with other.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0] == 0


def test_canonical_coding_builder_rejects_mismatched_ledger(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    other = SQLiteStore(tmp_path / "other.db")
    other.initialize()

    with pytest.raises(ValueError, match="must use the host SQLiteStore instance"):
        build_product_factory_coding_program_host(
            store,
            worker=_UnusedWorker(),
            contexts=_UnusedContexts(),
            evidence=_UnusedEvidence(),
            idempotency=IdempotencyLedger(other),
        )


def test_same_store_explicit_ledger_and_default_remain_supported(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    ledger = IdempotencyLedger(store)

    explicit = ProductFactoryProgramHost(
        store=store, worker=_UnusedWorker(), idempotency=ledger
    )
    assert explicit.idempotency is ledger
    assert explicit._ledger is ledger

    implicit = build_product_factory_coding_program_host(
        store,
        worker=_UnusedWorker(),
        contexts=_UnusedContexts(),
        evidence=_UnusedEvidence(),
    )
    assert implicit.idempotency is None
    assert isinstance(implicit._ledger, IdempotencyLedger)
    assert implicit._ledger._store is store


@pytest.mark.parametrize("different_path", [False, True])
def test_program_host_rejects_cross_store_work_ownership_before_worker_effect(
    tmp_path: Path,
    different_path: bool,
) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    other_path = tmp_path / ("ownership.db" if different_path else "factory.db")
    other = SQLiteStore(other_path)
    other.initialize()
    ownership = ProductFactoryWorkOwnership(other)

    with pytest.raises(ValueError, match="ownership must use the host SQLiteStore instance"):
        ProductFactoryProgramHost(
            store=store,
            worker=_UnusedWorker(),
            ownership=ownership,
        )

    with store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_factory_work_ownership"
        ).fetchone()[0] == 0
    with other.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_factory_work_ownership"
        ).fetchone()[0] == 0


def test_same_store_explicit_work_ownership_remains_supported(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    ownership = ProductFactoryWorkOwnership(store)

    host = ProductFactoryProgramHost(
        store=store,
        worker=_UnusedWorker(),
        ownership=ownership,
    )

    assert host.ownership is ownership
    assert host._ownership is ownership
    assert host._ownership._store is store

class _TruthinessForbiddenLedger(IdempotencyLedger):
    def __bool__(self) -> bool:
        raise AssertionError("idempotency truthiness must not execute")


class _TruthinessForbiddenOwnership(ProductFactoryWorkOwnership):
    def __bool__(self) -> bool:
        raise AssertionError("ownership truthiness must not execute")


def test_explicit_durable_authorities_are_selected_without_truthiness(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "factory.db")
    store.initialize()
    ledger = _TruthinessForbiddenLedger(store)
    ownership = _TruthinessForbiddenOwnership(store)

    host = ProductFactoryProgramHost(
        store=store,
        worker=_UnusedWorker(),
        idempotency=ledger,
        ownership=ownership,
    )

    assert host.idempotency is ledger
    assert host._ledger is ledger
    assert host.ownership is ownership
    assert host._ownership is ownership

