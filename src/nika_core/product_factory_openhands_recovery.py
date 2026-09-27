from __future__ import annotations

from dataclasses import dataclass

from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.toolsmith.contracts import RecoveryState

_PRODUCT_FACTORY_WORKER_OPERATION_TYPE = "product_factory.coding_worker"


@dataclass(frozen=True, slots=True)
class ProductFactoryOpenHandsRecoveryProbe:
    """Reconstruct fail-closed OpenHands state from Product Factory durable ledger.

    ProductFactoryProgramHost reserves pf-worker:<work_id> before dispatching any
    external coding worker. If Nika restarts while the worker process-local state is
    lost, the surviving ledger record proves that this work identity already crossed
    the durable dispatch boundary. It is therefore unsafe to replay automatically.
    """

    ledger: IdempotencyLedger

    async def inspect(self, job_id: str) -> RecoveryState | None:
        work_id = job_id.strip()
        if not work_id or work_id != job_id:
            raise ValueError("OpenHands recovery work id must be non-empty and canonical")

        record = self.ledger.get(f"pf-worker:{work_id}")
        if record is None:
            return None

        if record.operation_type != _PRODUCT_FACTORY_WORKER_OPERATION_TYPE:
            return RecoveryState(
                "manual_reconcile_required",
                "pf-ledger:operation-type-mismatch",
            )

        if record.status is IdempotencyStatus.PENDING:
            token = "pf-ledger:pending"
        elif record.status is IdempotencyStatus.UNCERTAIN:
            token = "pf-ledger:uncertain"
        else:
            # Durable result reconciliation belongs to ProductFactoryProgramHost;
            # without process-local exact result bytes the worker must not fabricate them.
            token = "pf-ledger:completed"
        return RecoveryState("manual_reconcile_required", token)
