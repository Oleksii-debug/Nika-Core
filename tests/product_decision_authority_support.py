from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductProjectRepository,
)
from nika_core.security import ApprovalAuthority


class AuthorizingProductDecisionRepository(ProductDecisionRepository):
    """Test fixture that issues real host approval for final ProductDecision writes."""

    def __init__(self, store: SQLiteStore) -> None:
        self._test_authority = ApprovalAuthority(
            issuer_id="test-product-owner-authority",
        )
        super().__init__(
            store,
            approval_verifier=self._test_authority.verifier(),
        )

    def record(
        self,
        project_id: str,
        decision: ProductDecision,
        *,
        expected_row_version: int,
        idempotency_key: str,
        approval=None,
        approval_task_id: str | None = None,
    ):
        if decision.state is not ProductDecisionState.PROPOSED and approval is None:
            approval_task_id = f"test-product-decision:{decision.decision_id}"
            intent = self.approval_intent(
                project_id,
                decision,
                expected_row_version=expected_row_version,
                task_id=approval_task_id,
            )
            request = self._test_authority.request(intent)
            approval = self._test_authority.approve(request.request_id)
        return super().record(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            approval=approval,
            approval_task_id=approval_task_id,
        )


class AuthorizingProductProjectCommandService(ProductProjectCommandService):
    """Test fixture that exercises PF5 final decisions through trusted approval."""

    def __init__(self, repository: ProductProjectRepository) -> None:
        self._test_authority = ApprovalAuthority(
            issuer_id="test-product-command-owner-authority",
        )
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
        approval=None,
        approval_task_id: str | None = None,
    ):
        if decision.state is not ProductDecisionState.PROPOSED and approval is None:
            approval_task_id = f"test-product-command:{decision.decision_id}"
            intent = self.decision_approval_intent(
                project_id,
                decision,
                expected_row_version=expected_row_version,
                task_id=approval_task_id,
            )
            request = self._test_authority.request(intent)
            approval = self._test_authority.approve(request.request_id)
        return super().record_decision(
            project_id,
            decision,
            expected_row_version=expected_row_version,
            idempotency_key=idempotency_key,
            approval=approval,
            approval_task_id=approval_task_id,
        )
