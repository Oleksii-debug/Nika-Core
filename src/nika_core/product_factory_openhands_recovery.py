from __future__ import annotations

from dataclasses import dataclass

from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.toolsmith.contracts import CodingJob, IsolationClass, RecoveryState
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRecoveryBinding,
    OpenHandsSandboxEndpoint,
)

_PRODUCT_FACTORY_WORKER_OPERATION_TYPE = "product_factory.coding_worker"
_OPENHANDS_BINDING_OPERATION_TYPE = "product_factory.openhands_recovery_binding"
_OPENHANDS_BINDING_SCHEMA = "nika-openhands-recovery-binding-v1"


def _canonical_work_id(job_id: str) -> str:
    if type(job_id) is not str:
        raise ValueError("OpenHands recovery work id must be an exact string")
    work_id = job_id.strip()
    if not work_id or work_id != job_id:
        raise ValueError("OpenHands recovery work id must be non-empty and canonical")
    return work_id


def _binding_key(work_id: str) -> str:
    return f"pf-openhands-binding:{work_id}"


def _binding_payload(binding: OpenHandsRecoveryBinding) -> dict[str, object]:
    endpoint = binding.endpoint
    return {
        "schema": _OPENHANDS_BINDING_SCHEMA,
        "job_id": binding.job_id,
        "endpoint_id": endpoint.endpoint_id,
        "host": endpoint.host,
        "working_dir": endpoint.working_dir,
        "isolation_class": endpoint.isolation_class.value,
        "sandbox_egress_hosts": list(endpoint.sandbox_egress_hosts),
        "network_policy_enforced": endpoint.network_policy_enforced,
        "fresh_workspace": endpoint.fresh_workspace,
        "conversation_id": binding.conversation_id,
        "agent_profile_id": binding.agent_profile_id,
    }


def _binding_from_payload(payload: object) -> OpenHandsRecoveryBinding:
    if type(payload) is not dict or payload.get("schema") != _OPENHANDS_BINDING_SCHEMA:
        raise ValueError("OpenHands durable recovery binding has an invalid schema")
    egress = payload.get("sandbox_egress_hosts")
    if type(egress) is not list or any(type(item) is not str for item in egress):
        raise ValueError("OpenHands durable recovery binding has invalid egress identity")
    try:
        isolation = IsolationClass(payload.get("isolation_class"))
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenHands durable recovery binding has invalid isolation") from exc
    endpoint = OpenHandsSandboxEndpoint(
        endpoint_id=payload.get("endpoint_id"),
        host=payload.get("host"),
        working_dir=payload.get("working_dir"),
        isolation_class=isolation,
        sandbox_egress_hosts=tuple(egress),
        network_policy_enforced=payload.get("network_policy_enforced"),
        fresh_workspace=payload.get("fresh_workspace"),
    )
    return OpenHandsRecoveryBinding(
        job_id=payload.get("job_id"),
        endpoint=endpoint,
        conversation_id=payload.get("conversation_id"),
        agent_profile_id=payload.get("agent_profile_id"),
    )


@dataclass(frozen=True, slots=True)
class ProductFactoryOpenHandsRecoveryProbe:
    """Durable Product Factory identity plus secret-free OpenHands recovery binding."""

    ledger: IdempotencyLedger

    def bind(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        conversation_id: str,
        agent_profile_id: str,
    ) -> OpenHandsRecoveryBinding:
        work_id = _canonical_work_id(job.job_id)
        worker_record = self.ledger.get(f"pf-worker:{work_id}")
        if worker_record is None:
            raise ValueError("Product Factory worker identity is not durably reserved")
        if worker_record.operation_type != _PRODUCT_FACTORY_WORKER_OPERATION_TYPE:
            raise ValueError("Product Factory worker operation type does not match")
        if worker_record.status is not IdempotencyStatus.PENDING:
            raise IdempotencyConflictError(
                "OpenHands recovery binding requires a pending Product Factory worker"
            )

        binding = OpenHandsRecoveryBinding(
            job_id=work_id,
            endpoint=endpoint,
            conversation_id=conversation_id,
            agent_profile_id=agent_profile_id,
        )
        operation_key = _binding_key(work_id)
        record, created = self.ledger.reserve_once(
            operation_key=operation_key,
            task_id=worker_record.task_id,
            operation_type=_OPENHANDS_BINDING_OPERATION_TYPE,
            input_fingerprint=binding.opaque_token,
        )
        if created:
            record = self.ledger.complete(operation_key, _binding_payload(binding))
        if record.status is not IdempotencyStatus.COMPLETED:
            raise IdempotencyConflictError(
                "OpenHands recovery binding is not durably complete"
            )
        loaded = self._binding_from_record(record)
        if loaded != binding:
            raise IdempotencyConflictError(
                "OpenHands recovery binding differs from durable identity"
            )
        return loaded

    def load(self, job_id: str) -> OpenHandsRecoveryBinding | None:
        work_id = _canonical_work_id(job_id)
        record = self.ledger.get(_binding_key(work_id))
        if record is None:
            return None
        return self._binding_from_record(record)

    def _binding_from_record(self, record) -> OpenHandsRecoveryBinding:
        if record.operation_type != _OPENHANDS_BINDING_OPERATION_TYPE:
            raise ValueError("OpenHands durable binding operation type does not match")
        if record.status is not IdempotencyStatus.COMPLETED:
            raise ValueError("OpenHands durable recovery binding is incomplete")
        if record.result is None:
            raise ValueError("OpenHands durable recovery binding has no payload")
        binding = _binding_from_payload(dict(record.result))
        if record.input_fingerprint != binding.opaque_token:
            raise ValueError("OpenHands durable recovery binding fingerprint mismatch")
        worker_record = self.ledger.get(f"pf-worker:{binding.job_id}")
        if worker_record is None or worker_record.task_id != record.task_id:
            raise ValueError("OpenHands durable binding is detached from Product Factory work")
        return binding

    async def inspect(self, job_id: str) -> RecoveryState | None:
        work_id = _canonical_work_id(job_id)
        record = self.ledger.get(f"pf-worker:{work_id}")
        if record is None:
            return None

        if record.operation_type != _PRODUCT_FACTORY_WORKER_OPERATION_TYPE:
            return RecoveryState(
                "manual_reconcile_required",
                "pf-ledger:operation-type-mismatch",
            )

        if record.status in {IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN}:
            try:
                binding = self.load(work_id)
            except Exception:  # noqa: BLE001 - durable binding corruption fails closed
                return RecoveryState(
                    "manual_reconcile_required",
                    "pf-ledger:binding-invalid",
                )
            if binding is not None:
                return RecoveryState(
                    "remote_reconcile_required",
                    binding.opaque_token,
                )
            token = (
                "pf-ledger:pending"
                if record.status is IdempotencyStatus.PENDING
                else "pf-ledger:uncertain"
            )
            return RecoveryState("manual_reconcile_required", token)

        return RecoveryState("manual_reconcile_required", "pf-ledger:completed")
