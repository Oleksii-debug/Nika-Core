from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)

from .product_factory_operations_contracts import (
    DeployableService,
    MaintenanceEffectReservation,
    MaintenanceEffectState,
    MaintenanceRequest,
    MaintenanceResult,
    ProductOperationsError,
)

_OPERATION_TYPE = "product_operations.maintenance"
_RESULT_SCHEMA = "nika-pf8-maintenance-result-v1"


class RuntimeIdempotencyMaintenanceJournal:
    """Adapt the canonical runtime ledger to PF8 maintenance side effects.

    ``task_id`` is supplied by the host and must already exist in the canonical task store.
    This adapter never invents a task identity and never creates a second persistence system.
    """

    def __init__(self, ledger: IdempotencyLedger, *, task_id: str) -> None:
        if not task_id.strip():
            raise ProductOperationsError("maintenance effect journal task_id must not be empty")
        self._ledger = ledger
        self._task_id = task_id

    def reserve(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> MaintenanceEffectReservation:
        operation_key = self._operation_key(project_id, request.request_id)
        fingerprint = self._fingerprint(project_id, service, request)
        try:
            record, created = self._ledger.reserve_once(
                operation_key=operation_key,
                task_id=self._task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=fingerprint,
            )
        except (IdempotencyConflictError, sqlite3.Error, KeyError, ValueError) as exc:
            raise ProductOperationsError(
                "maintenance effect reservation conflicts with durable runtime authority"
            ) from exc
        return self._reservation(record, created=created)

    def lookup(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> MaintenanceEffectReservation | None:
        operation_key = self._operation_key(project_id, request.request_id)
        fingerprint = self._fingerprint(project_id, service, request)
        try:
            record = self._ledger.get(operation_key)
            if record is None:
                return None
            if (
                record.task_id != self._task_id
                or record.operation_type != _OPERATION_TYPE
                or record.input_fingerprint != fingerprint
            ):
                raise ProductOperationsError(
                    "maintenance effect identity conflicts with durable runtime authority"
                )
            return self._reservation(record, created=False)
        except ProductOperationsError:
            raise
        except (sqlite3.Error, KeyError, ValueError) as exc:
            raise ProductOperationsError(
                "maintenance effect lookup conflicts with durable runtime authority"
            ) from exc

    def complete(self, operation_key: str, result: MaintenanceResult) -> None:
        try:
            self._ledger.complete(operation_key, self._encode_result(result))
        except (IdempotencyConflictError, sqlite3.Error, KeyError, ValueError) as exc:
            raise ProductOperationsError(
                "maintenance effect completion could not be committed durably"
            ) from exc

    def mark_uncertain(self, operation_key: str) -> None:
        try:
            current = self._ledger.require(operation_key)
            if current.status is IdempotencyStatus.UNCERTAIN:
                return
            self._ledger.mark_uncertain(operation_key)
        except (IdempotencyConflictError, sqlite3.Error, KeyError, ValueError) as exc:
            raise ProductOperationsError(
                "maintenance effect uncertainty could not be committed durably"
            ) from exc

    def reconcile(self, operation_key: str, result: MaintenanceResult) -> None:
        encoded = self._encode_result(result)
        try:
            current = self._ledger.require(operation_key)
            if current.status is IdempotencyStatus.PENDING:
                self._ledger.complete(operation_key, encoded)
                return
            if current.status is IdempotencyStatus.UNCERTAIN:
                self._ledger.reconcile_completed(operation_key, encoded)
                return
            durable = self._decode_result(current.result)
            if durable != result:
                raise ProductOperationsError(
                    "maintenance reconciliation conflicts with durable completed result"
                )
        except ProductOperationsError:
            raise
        except (IdempotencyConflictError, sqlite3.Error, KeyError, ValueError) as exc:
            raise ProductOperationsError(
                "maintenance effect reconciliation conflicts with durable runtime authority"
            ) from exc

    def _reservation(
        self,
        record: IdempotencyRecord,
        *,
        created: bool,
    ) -> MaintenanceEffectReservation:
        return MaintenanceEffectReservation(
            operation_key=record.operation_key,
            state=MaintenanceEffectState(record.status.value),
            created=created,
            result=self._decode_result(record.result)
            if record.status is IdempotencyStatus.COMPLETED
            else None,
        )

    @staticmethod
    def _operation_key(project_id: str, request_id: str) -> str:
        if not project_id.strip() or not request_id.strip():
            raise ProductOperationsError("maintenance effect identity must not be empty")
        identity = json.dumps(
            {"project_id": project_id, "request_id": request_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"pf8-maintenance:{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _fingerprint(
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> str:
        payload = {
            "project_id": project_id,
            "service": {
                "credential_refs": list(service.credential_refs),
                "dependencies": list(service.dependencies),
                "environment_id": service.environment_id,
                "min_healthy_replicas": service.min_healthy_replicas,
                "project_id": service.project_id,
                "release_sha": service.release_sha,
                "replicas": [
                    {"node_id": item.node_id, "replica_id": item.replica_id}
                    for item in service.replicas
                ],
                "service_id": service.service_id,
                "wave": service.wave,
            },
            "request": {
                "action": request.action.value,
                "approval_ref": request.approval_ref,
                "evidence_refs": list(request.evidence_refs),
                "reason": request.reason,
                "request_id": request.request_id,
                "service_id": request.service_id,
            },
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _encode_result(result: MaintenanceResult) -> dict[str, object]:
        return {
            "schema": _RESULT_SCHEMA,
            "applied": result.applied,
            "uncertain": result.uncertain,
            "evidence_refs": list(result.evidence_refs),
        }

    @staticmethod
    def _decode_result(value: Mapping[str, object] | None) -> MaintenanceResult:
        if value is None or set(value) != {
            "schema",
            "applied",
            "uncertain",
            "evidence_refs",
        }:
            raise ProductOperationsError("durable maintenance result schema is invalid")
        if value["schema"] != _RESULT_SCHEMA:
            raise ProductOperationsError("durable maintenance result schema is unsupported")
        evidence_refs = value["evidence_refs"]
        if not isinstance(evidence_refs, list) or any(
            type(item) is not str for item in evidence_refs
        ):
            raise ProductOperationsError("durable maintenance result evidence is invalid")
        return MaintenanceResult(
            applied=value["applied"],  # type: ignore[arg-type]
            uncertain=value["uncertain"],  # type: ignore[arg-type]
            evidence_refs=tuple(evidence_refs),
        )


__all__ = ["RuntimeIdempotencyMaintenanceJournal"]
