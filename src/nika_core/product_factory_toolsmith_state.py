from __future__ import annotations

from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_toolsmith_state_impl import (
    ComponentCapabilityBinding,
    ComponentCapabilityBindingState,
    ProductFactoryToolsmithBindingError,
    ProductFactoryToolsmithBindingRepository as _BindingRepository,
)

_DURABLE_REASON = "Product Factory worker capability gap"
_DURABLE_ATTEMPTED_METHODS: tuple[str, ...] = ()


class ProductFactoryToolsmithBindingRepository(_BindingRepository):
    """Persist only authoritative gap identity, never caller-controlled diagnostics."""

    def reserve(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...],
    ) -> ComponentCapabilityBinding:
        if not host_task_id.strip() or not capability_id.strip() or not reason.strip():
            raise ProductFactoryToolsmithBindingError(
                "host task, capability id and reason must not be empty"
            )
        if any(not method.strip() for method in attempted_methods):
            raise ProductFactoryToolsmithBindingError(
                "attempted methods must not be empty"
            )

        return super().reserve(
            host_task_id=host_task_id,
            request=request,
            capability_id=capability_id,
            reason=_DURABLE_REASON,
            attempted_methods=_DURABLE_ATTEMPTED_METHODS,
        )
