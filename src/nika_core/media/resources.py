from __future__ import annotations

from dataclasses import dataclass

from nika_core.media.contracts import MediaResourceClaim, ResourceClass
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.resources.contracts import ResourceBudget, ResourceObserverPort
from nika_core.resources.manager import ResourceDecision, ResourceManager


@dataclass(frozen=True, slots=True)
class MediaResourceLease:
    scope: str
    owner_id: str
    request_id: str


class MediaResourceCoordinator:
    """Thin DEV05 binding over the canonical ResourceManager.

    Heavy media-model work uses a shared machine-level owner so only one heavy resident may be
    granted at once by default. Media-specific minimum-memory and cross-class exclusion policy
    stays in this adapter rather than widening the shared ResourceManager contract.
    """

    def __init__(
        self,
        manager: ResourceManager,
        observer: ResourceObserverPort | None = None,
    ) -> None:
        self._manager = manager
        self._observer = observer
        self._active_claims: dict[str, MediaResourceClaim] = {}

    def request(self, claim: MediaResourceClaim) -> MediaResourceLease:
        if claim.claim_id in self._active_claims:
            active = self._active_claims[claim.claim_id]
            if active != claim:
                raise MediaError(
                    MediaErrorCode.RESOURCE_BLOCKED,
                    "media resource claim_id is already active with different policy",
                    retryable=False,
                )
            scope, owner_id = self._scope_for(claim)
            return MediaResourceLease(scope=scope, owner_id=owner_id, request_id=claim.claim_id)

        self._validate_local_policy(claim)
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
            self._manager.cancel_waiting(
                scope=scope,
                owner_id=owner_id,
                request_id=claim.claim_id,
            )
            raise MediaError(
                MediaErrorCode.RESOURCE_BLOCKED,
                f"media resource claim blocked: {decision.reason}",
                retryable=True,
            )
        self._active_claims[claim.claim_id] = claim
        return MediaResourceLease(scope=scope, owner_id=owner_id, request_id=claim.claim_id)

    def release(self, lease: MediaResourceLease) -> bool:
        released = self._manager.release(
            scope=lease.scope,
            owner_id=lease.owner_id,
            request_id=lease.request_id,
        )
        if released:
            self._active_claims.pop(lease.request_id, None)
        return released

    def _validate_local_policy(self, claim: MediaResourceClaim) -> None:
        if claim.min_available_memory_bytes is not None:
            if self._observer is None:
                raise MediaError(
                    MediaErrorCode.RESOURCE_BLOCKED,
                    "minimum available-memory policy cannot be verified without a resource observer",
                    retryable=True,
                )
            available = self._observer.snapshot().available_memory_bytes
            if available < claim.min_available_memory_bytes:
                raise MediaError(
                    MediaErrorCode.RESOURCE_BLOCKED,
                    "media resource claim blocked: insufficient_available_memory",
                    retryable=True,
                )

        for active in self._active_claims.values():
            candidate_blocks_active = active.resource_class in claim.mutually_exclusive_with
            active_blocks_candidate = claim.resource_class in active.mutually_exclusive_with
            if candidate_blocks_active or active_blocks_candidate:
                raise MediaError(
                    MediaErrorCode.RESOURCE_BLOCKED,
                    "media resource claim blocked: mutually_exclusive_resource_class",
                    retryable=True,
                )

    @staticmethod
    def _scope_for(claim: MediaResourceClaim) -> tuple[str, str]:
        if claim.resource_class == ResourceClass.HEAVY_MODEL:
            return ("media_heavy_model", "local_machine")
        return (f"media_{claim.resource_class.value}", claim.owner_id)
