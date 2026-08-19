from __future__ import annotations

from dataclasses import dataclass

from nika_core.media.contracts import MediaResourceClaim, ResourceClass
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.resources.contracts import ResourceBudget
from nika_core.resources.manager import ResourceDecision, ResourceManager


@dataclass(frozen=True, slots=True)
class MediaResourceLease:
    scope: str
    owner_id: str
    request_id: str


class MediaResourceCoordinator:
    """Thin DEV05 binding over the canonical ResourceManager.

    Heavy media-model work uses a shared machine-level owner so only one heavy resident may be
    granted at once by default. Batch A itself only establishes the contract; ASR/OCR adapters
    consume it in later batches.
    """

    def __init__(self, manager: ResourceManager) -> None:
        self._manager = manager

    def request(self, claim: MediaResourceClaim) -> MediaResourceLease:
        scope, owner_id = self._scope_for(claim)
        self._manager.set_budget(
            ResourceBudget(
                scope=scope,
                owner_id=owner_id,
                max_concurrent=claim.max_concurrent,
            )
        )
        decision: ResourceDecision = self._manager.request(
            scope=scope,
            owner_id=owner_id,
            request_id=claim.claim_id,
        )
        if not decision.granted:
            raise MediaError(
                MediaErrorCode.RESOURCE_BLOCKED,
                f"media resource claim blocked: {decision.reason}",
                retryable=True,
            )
        return MediaResourceLease(scope=scope, owner_id=owner_id, request_id=claim.claim_id)

    def release(self, lease: MediaResourceLease) -> bool:
        return self._manager.release(
            scope=lease.scope,
            owner_id=lease.owner_id,
            request_id=lease.request_id,
        )

    @staticmethod
    def _scope_for(claim: MediaResourceClaim) -> tuple[str, str]:
        if claim.resource_class == ResourceClass.HEAVY_MODEL:
            return ("media_heavy_model", "local_machine")
        return (f"media_{claim.resource_class.value}", claim.owner_id)
