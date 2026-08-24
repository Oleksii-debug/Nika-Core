from __future__ import annotations

from nika_core.product_factory_operations_contracts import (
    DeployableService,
    MaintenanceEffectReservation,
    MaintenanceEffectState,
    MaintenanceRequest,
    MaintenanceResult,
    ProductOperationsError,
)


class MemoryEffectJournal:
    """Deterministic in-memory contract fake; never production durability evidence."""

    def __init__(self) -> None:
        self._records: dict[
            str,
            tuple[tuple[object, ...], MaintenanceEffectState, MaintenanceResult | None],
        ] = {}

    def reserve(
        self,
        *,
        project_id: str,
        service: DeployableService,
        request: MaintenanceRequest,
    ) -> MaintenanceEffectReservation:
        key = f"test-pf8:{project_id}:{request.request_id}"
        fingerprint = (
            project_id,
            service,
            request,
        )
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = (fingerprint, MaintenanceEffectState.PENDING, None)
            return MaintenanceEffectReservation(
                key,
                MaintenanceEffectState.PENDING,
                True,
            )
        prior_fingerprint, state, result = existing
        if prior_fingerprint != fingerprint:
            raise ProductOperationsError("test maintenance effect identity conflict")
        return MaintenanceEffectReservation(key, state, False, result)

    def complete(self, operation_key: str, result: MaintenanceResult) -> None:
        fingerprint, state, _ = self._require(operation_key)
        if state is not MaintenanceEffectState.PENDING:
            raise ProductOperationsError("test maintenance effect cannot complete from state")
        self._records[operation_key] = (
            fingerprint,
            MaintenanceEffectState.COMPLETED,
            result,
        )

    def mark_uncertain(self, operation_key: str) -> None:
        fingerprint, state, _ = self._require(operation_key)
        if state is MaintenanceEffectState.COMPLETED:
            raise ProductOperationsError("test completed maintenance effect cannot reopen")
        self._records[operation_key] = (
            fingerprint,
            MaintenanceEffectState.UNCERTAIN,
            None,
        )

    def reconcile(self, operation_key: str, result: MaintenanceResult) -> None:
        fingerprint, state, prior = self._require(operation_key)
        if state is MaintenanceEffectState.COMPLETED:
            if prior != result:
                raise ProductOperationsError("test maintenance reconciliation conflict")
            return
        self._records[operation_key] = (
            fingerprint,
            MaintenanceEffectState.COMPLETED,
            result,
        )

    def _require(
        self,
        operation_key: str,
    ) -> tuple[tuple[object, ...], MaintenanceEffectState, MaintenanceResult | None]:
        try:
            return self._records[operation_key]
        except KeyError as exc:
            raise ProductOperationsError("unknown test maintenance effect") from exc
