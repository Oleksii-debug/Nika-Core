from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
)
from nika_core.product_decisions import ProductDecisionRepository, StoredProductDecision
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductProjectRepository,
)
from nika_core.security import ApprovalAuthority, ApprovalEvidence

_NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def _issue(authority: ApprovalAuthority, intent: Any) -> ApprovalEvidence:
    request = authority.request(intent, now=_NOW)
    return authority.approve(request.request_id, now=_NOW + timedelta(seconds=1))


class ApprovedProductDecisionRepository(ProductDecisionRepository):
    """Test harness that simulates explicit trusted-host owner approval."""

    def __init__(self, store: Any) -> None:
        self._test_authority = ApprovalAuthority()
        super().__init__(store, approval_verifier=self._test_authority.verifier())

    def record(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
        approval: ApprovalEvidence | None = None,
        now: datetime | None = None,
    ) -> StoredProductDecision:
        if decision.state is ProductDecisionState.APPROVED and approval is None:
            try:
                return super().record(
                    project_id,
                    decision,
                    expected_row_version=expected_row_version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
            except PermissionError as exc:
                if "trusted product-owner approval" not in str(exc):
                    raise
            intent = self.approval_intent(
                project_id,
                decision,
                expected_row_version=expected_row_version,
                idempotency_key=idempotency_key,
            )
            approval = _issue(self._test_authority, intent)
            now = _NOW + timedelta(seconds=2)
        return super().record(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            approval=approval,
            now=now,
        )


class ApprovedProductProjectCommandService(ProductProjectCommandService):
    """PF5 test harness using the same trusted approval path as production."""

    def __init__(self, repository: ProductProjectRepository) -> None:
        self._test_authority = ApprovalAuthority()
        super().__init__(
            repository,
            approval_verifier=self._test_authority.verifier(),
        )

    def record_decision(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
        approval: ApprovalEvidence | None = None,
        now: datetime | None = None,
    ):
        if decision.state is ProductDecisionState.APPROVED and approval is None:
            try:
                return super().record_decision(
                    project_id,
                    decision,
                    expected_row_version=expected_row_version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
            except PermissionError as exc:
                if "trusted product-owner approval" not in str(exc):
                    raise
            intent = self.decision_approval_intent(
                project_id,
                decision,
                expected_row_version=expected_row_version,
                idempotency_key=idempotency_key,
            )
            approval = _issue(self._test_authority, intent)
            now = _NOW + timedelta(seconds=2)
        return super().record_decision(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            approval=approval,
            now=now,
        )
