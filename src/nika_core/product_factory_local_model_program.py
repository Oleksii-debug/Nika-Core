from __future__ import annotations

import pathlib
from collections.abc import Mapping

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.product_factory_local_coding import (
    ContainedLocalCodingPolicy,
    ContainedLocalCodingProgram,
    build_contained_local_coding_program,
)
from nika_core.product_factory_local_coding_planner import ModelGatewayLocalCodingPlanner
from nika_core.product_factory_orchestration import TeamPlan
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityPort,
    ReviewerPrincipalBindings,
)
from nika_core.runtime.idempotency import IdempotencyLedger


def build_modelgateway_contained_local_coding_program(
    store: SQLiteStore,
    *,
    workspace_parent: pathlib.Path,
    repositories: Mapping[str, pathlib.Path],
    gateway: ModelGateway,
    provider_id: str | None,
    provider_kind: ProviderKind | None,
    model: str | None,
    policy: ContainedLocalCodingPolicy,
    fallback_provider_ids: tuple[str, ...] = (),
    model_timeout_seconds: float = 60.0,
    idempotency: IdempotencyLedger | None = None,
    review_evidence_authority: ProductFactoryReviewAuthorityPort | None = None,
    team_plan: TeamPlan | None = None,
    reviewer_principals: ReviewerPrincipalBindings = (),
    git_executable: str = "git",
    planner_source_environment: Mapping[str, str] | None = None,
) -> ContainedLocalCodingProgram:
    """Compose the canonical local Product Factory with the canonical ModelGateway.

    Repository and model-route authority are explicit trusted-host inputs. The helper
    introduces no worker/runtime/store of its own: it builds the bounded
    ModelGatewayLocalCodingPlanner and delegates all mutation, recovery, evidence,
    review and multi-repository execution wiring to build_contained_local_coding_program.
    """

    planner = ModelGatewayLocalCodingPlanner(
        gateway=gateway,
        repositories=repositories,
        provider_id=provider_id,
        provider_kind=provider_kind,
        model=model,
        fallback_provider_ids=fallback_provider_ids,
        timeout_seconds=model_timeout_seconds,
        git_executable=git_executable,
        source_environment=planner_source_environment,
    )
    return build_contained_local_coding_program(
        store,
        workspace_parent=workspace_parent,
        repositories=repositories,
        planner=planner,
        policy=policy,
        idempotency=idempotency,
        review_evidence_authority=review_evidence_authority,
        team_plan=team_plan,
        reviewer_principals=reviewer_principals,
        git_executable=git_executable,
    )
