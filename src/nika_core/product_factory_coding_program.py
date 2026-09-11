from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerComponentAdapter,
    CodingWorkerContextPort,
    CodingWorkerEvidencePort,
)
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.toolsmith.contracts import CodingWorkerPort


def build_product_factory_coding_program_host(
    store: SQLiteStore,
    *,
    worker: CodingWorkerPort,
    contexts: CodingWorkerContextPort,
    evidence: CodingWorkerEvidencePort,
    idempotency: IdempotencyLedger | None = None,
) -> ProductFactoryProgramHost:
    """Compose the canonical durable Product Factory host with the canonical coding worker.

    This is intentionally a thin composition root. Workspace/lease policy and exact
    repository evidence remain supplied by the trusted host ports; worker execution stays
    behind the existing Toolsmith ``CodingWorkerPort``. The function exists so production
    callers do not need to reconstruct the test-only adapter/host wiring or accidentally
    bypass the crash-consistent ``ProductFactoryProgramHost`` boundary.
    """

    adapter = CodingWorkerComponentAdapter(
        worker=worker,
        contexts=contexts,
        evidence=evidence,
    )
    return ProductFactoryProgramHost(
        store=store,
        worker=adapter,
        idempotency=idempotency,
    )
