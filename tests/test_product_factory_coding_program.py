from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_program import (
    build_product_factory_coding_program_host,
)
from nika_core.product_factory_coding_worker_adapter import CodingWorkerComponentAdapter
from nika_core.runtime.idempotency import IdempotencyLedger


class _Worker:
    async def execute(self, job):  # pragma: no cover - composition-only test
        raise AssertionError(job)

    async def cancel(self, job_id):  # pragma: no cover - composition-only test
        raise AssertionError(job_id)

    async def inspect(self, job_id):  # pragma: no cover - composition-only test
        raise AssertionError(job_id)

    async def recover(self, job, state):  # pragma: no cover - composition-only test
        raise AssertionError((job, state))


class _Contexts:
    async def context_for(self, request):  # pragma: no cover - composition-only test
        raise AssertionError(request)


class _Evidence:
    async def collect(self, request, job, result):  # pragma: no cover - composition-only test
        raise AssertionError((request, job, result))


def test_builder_reuses_canonical_adapter_and_durable_program_host(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    worker = _Worker()
    contexts = _Contexts()
    evidence = _Evidence()

    host = build_product_factory_coding_program_host(
        store,
        worker=worker,
        contexts=contexts,
        evidence=evidence,
    )

    assert host.store is store
    assert isinstance(host.worker, CodingWorkerComponentAdapter)
    assert host.worker.worker is worker
    assert host.worker.contexts is contexts
    assert host.worker.evidence is evidence


def test_builder_preserves_explicit_idempotency_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ledger = IdempotencyLedger(store)

    host = build_product_factory_coding_program_host(
        store,
        worker=_Worker(),
        contexts=_Contexts(),
        evidence=_Evidence(),
        idempotency=ledger,
    )

    assert host.idempotency is ledger
