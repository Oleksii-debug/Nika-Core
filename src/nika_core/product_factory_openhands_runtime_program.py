from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

import httpx

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_openhands_program import (
    OpenHandsProductFactoryError,
    OpenHandsProductFactoryPolicy,
    OpenHandsProductFactoryProgram,
    build_openhands_product_factory_program,
)
from nika_core.product_factory_openhands_runtime_api import (
    OpenHandsRuntimeApiConfig,
    OpenHandsRuntimeApiSandboxProvider,
)
from nika_core.product_factory_orchestration import TeamPlan
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityPort,
    ReviewerPrincipalBindings,
)
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.toolsmith.contracts import CodingJob
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsSandboxEndpoint,
    SandboxedAcceptanceRuntimePort,
)


def build_openhands_runtime_api_product_factory_program(
    store: SQLiteStore,
    *,
    workspace_parent: pathlib.Path,
    repositories: Mapping[str, pathlib.Path],
    runtime_api_config: OpenHandsRuntimeApiConfig,
    control_client_factory: Callable[[], httpx.Client],
    agent_profile_id_factory: Callable[[CodingJob, OpenHandsSandboxEndpoint], str],
    acceptance_runtime: SandboxedAcceptanceRuntimePort,
    policy: OpenHandsProductFactoryPolicy,
    agent_client_factory: Callable[[str, str], httpx.Client] | None = None,
    idempotency: IdempotencyLedger | None = None,
    review_evidence_authority: ProductFactoryReviewAuthorityPort | None = None,
    team_plan: TeamPlan | None = None,
    reviewer_principals: ReviewerPrincipalBindings = (),
    git_executable: str = "git",
    source_environment: Mapping[str, str] | None = None,
    max_iterations: int = 96,
    poll_interval_seconds: float = 0.2,
) -> OpenHandsProductFactoryProgram:
    """Compose the canonical OpenHands Product Factory through one Runtime API authority.

    The Runtime API provider owns both sandbox provisioning and redemption of the
    Agent Server session credential. Its exact client_for method is passed to the
    incumbent Agent Server runtime, so callers cannot accidentally pair one sandbox
    authority with an unrelated Agent Server authentication source.

    Network authority remains explicit on the incumbent Product Factory policy. Every
    trusted Runtime API host, admitted Agent Server host and declared sandbox-egress
    host must already be present in that policy before the program is constructed.
    """

    if type(runtime_api_config) is not OpenHandsRuntimeApiConfig:
        raise OpenHandsProductFactoryError(
            "OpenHands Runtime API composition requires the exact config carrier"
        )
    if type(policy) is not OpenHandsProductFactoryPolicy:
        raise OpenHandsProductFactoryError(
            "OpenHands Runtime API composition requires the exact Product Factory policy"
        )

    policy.__post_init__()
    runtime_api_config.__post_init__()
    required_hosts = _required_runtime_hosts(runtime_api_config)
    approved_hosts = frozenset(policy.approved_hosts)
    missing_hosts = tuple(host for host in required_hosts if host not in approved_hosts)
    if missing_hosts:
        joined = ", ".join(missing_hosts)
        raise OpenHandsProductFactoryError(
            f"OpenHands Runtime API composition is missing approved hosts: {joined}"
        )

    provider = OpenHandsRuntimeApiSandboxProvider(
        runtime_api_config,
        control_client_factory=control_client_factory,
        agent_client_factory=agent_client_factory,
    )
    return build_openhands_product_factory_program(
        store,
        workspace_parent=workspace_parent,
        repositories=repositories,
        sandbox_provider=provider,
        client_factory=provider.client_for,
        agent_profile_id_factory=agent_profile_id_factory,
        acceptance_runtime=acceptance_runtime,
        policy=policy,
        idempotency=idempotency,
        review_evidence_authority=review_evidence_authority,
        team_plan=team_plan,
        reviewer_principals=reviewer_principals,
        git_executable=git_executable,
        source_environment=source_environment,
        max_iterations=max_iterations,
        poll_interval_seconds=poll_interval_seconds,
    )


def _required_runtime_hosts(config: OpenHandsRuntimeApiConfig) -> tuple[str, ...]:
    runtime_host = urlparse(config.runtime_api_url).hostname
    if runtime_host is None:
        raise OpenHandsProductFactoryError("OpenHands Runtime API authority has no host")

    ordered = (
        runtime_host.casefold().rstrip("."),
        *config.agent_server_hosts,
        *config.sandbox_egress_hosts,
    )
    return tuple(dict.fromkeys(ordered))
