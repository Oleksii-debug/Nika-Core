from __future__ import annotations

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentRecord,
    DeploymentState,
    EnvironmentTier,
    ExecutionRegistrySnapshot,
)


class DeploymentPresentationIntegrityError(ValueError):
    """Raised when PF3 snapshot state is not safely presentable by PF5."""


def execution_status_entries(
    snapshot: ExecutionRegistrySnapshot,
) -> tuple[ProductStatusEntry, ...]:
    """Project integrated PF3 execution-node state into PF5 textual presentation."""
    active_leases: dict[str, int] = {}
    for lease in snapshot.leases:
        active_leases[lease.node_id] = active_leases.get(lease.node_id, 0) + 1

    entries: list[ProductStatusEntry] = []
    for node in snapshot.nodes:
        identity = node.identity
        features = ", ".join(sorted(node.capabilities.features)) or "none"
        toolchains = ", ".join(sorted(node.capabilities.toolchains)) or "none"
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id=f"execution-node:{identity.node_id}",
                label=f"Execution node {identity.node_id}",
                state="enabled" if node.enabled else "disabled",
                detail=(
                    f"Platform: {identity.platform.value}; architecture: {identity.architecture}; "
                    f"instance: {identity.instance_id}; features: {features}; "
                    f"toolchains: {toolchains}; gpu: {'yes' if node.capabilities.gpu else 'no'}; "
                    f"resources: {node.resources.cpu_cores} CPU / "
                    f"{node.resources.memory_mb} MiB RAM / {node.resources.disk_mb} MiB disk; "
                    f"active leases: {active_leases.get(identity.node_id, 0)}"
                ),
            )
        )
    for lease in snapshot.leases:
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.BUILD,
                item_id=f"execution-lease:{lease.lease_id}",
                label=f"Execution lease {lease.work_id}",
                state="leased",
                owner=lease.node_id,
                detail=(
                    f"Project: {lease.project_id}; work: {lease.work_id}; node: {lease.node_id}; "
                    f"issued: {lease.issued_at.isoformat()}; expires: {lease.expires_at.isoformat()}"
                ),
            )
        )
    return tuple(entries)


def deployment_status_entries(
    snapshot: DeploymentFabricSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    """Project integrated PF3 deployment/health/rollback state without provider secrets."""
    _validate_snapshot_backing(snapshot)
    entries: list[ProductStatusEntry] = []
    for record in snapshot.records:
        entries.extend(_record_entries(record))
    return tuple(entries)


def _validate_snapshot_backing(snapshot: DeploymentFabricSnapshot) -> None:
    """Mirror PF3 restore trust checks for release markers before presentation."""
    staging_projects: set[str] = set()
    for project_id, source_sha in snapshot.healthy_staging:
        if project_id in staging_projects:
            raise DeploymentPresentationIntegrityError(
                "deployment snapshot contains duplicate healthy-staging state"
            )
        staging_projects.add(project_id)
        if not any(
            _is_healthy_record(
                record,
                project_id=project_id,
                environment_id=None,
                source_sha=source_sha,
                require_staging=True,
            )
            for record in snapshot.records
        ):
            raise DeploymentPresentationIntegrityError(
                "healthy staging marker is not backed by a healthy staging deployment"
            )

    current_keys: set[tuple[str, str]] = set()
    for entry in snapshot.current_releases:
        if len(entry) != 3:
            raise DeploymentPresentationIntegrityError(
                "presentation requires normalized project/environment current-release identity"
            )
        project_id, environment_id, source_sha = entry
        key = (project_id, environment_id)
        if key in current_keys:
            raise DeploymentPresentationIntegrityError(
                "deployment snapshot contains duplicate current-release state"
            )
        current_keys.add(key)
        if not any(
            _is_healthy_record(
                record,
                project_id=project_id,
                environment_id=environment_id,
                source_sha=source_sha,
                require_staging=False,
            )
            for record in snapshot.records
        ):
            raise DeploymentPresentationIntegrityError(
                "current release marker is not backed by a healthy deployment"
            )


def _is_healthy_record(
    record: DeploymentRecord,
    *,
    project_id: str,
    environment_id: str | None,
    source_sha: str,
    require_staging: bool,
) -> bool:
    intent = record.intent
    if (
        record.state is not DeploymentState.HEALTHY
        or record.health is None
        or not record.health.healthy
        or intent.project_id != project_id
        or intent.release.source_sha != source_sha
    ):
        return False
    if environment_id is not None and intent.environment.environment_id != environment_id:
        return False
    return not require_staging or intent.environment.tier is EnvironmentTier.STAGING


def _record_entries(record: DeploymentRecord) -> tuple[ProductStatusEntry, ...]:
    intent = record.intent
    release = intent.release
    environment = intent.environment

    release_entry = ProductStatusEntry(
        kind=ProductStatusKind.RELEASE,
        item_id=f"release:{intent.intent_id}",
        label=f"Release {release.version}",
        state="candidate",
        detail=(
            f"Project: {release.project_id}; source SHA: {release.source_sha}; "
            f"environment: {environment.environment_id}; tier: {environment.tier.value}"
        ),
        evidence=(
            EvidenceReference(
                kind="git_commit",
                reference=release.source_sha,
                label="Exact release source SHA",
            ),
            EvidenceReference(
                kind="artifact_digest",
                reference=f"sha256:{release.artifact_digest}",
                sha256=release.artifact_digest,
                label="Release artifact SHA-256",
            ),
        ),
    )
    deployment_entry = ProductStatusEntry(
        kind=ProductStatusKind.DEPLOYMENT,
        item_id=f"deployment:{intent.intent_id}",
        label=f"Deployment {environment.environment_id}",
        state=record.state.value,
        detail=_deployment_detail(record),
        evidence=_deployment_evidence(record),
    )

    entries: list[ProductStatusEntry] = [release_entry, deployment_entry]
    if record.state in {
        DeploymentState.UNCERTAIN,
        DeploymentState.REJECTED,
    }:
        entries.append(
            ProductStatusEntry(
                kind=ProductStatusKind.BLOCKER,
                item_id=f"deployment:{intent.intent_id}:blocker",
                label=f"Deployment blocker {environment.environment_id}",
                state="active",
                detail=(
                    "Deployment requires reconciliation."
                    if record.state is DeploymentState.UNCERTAIN
                    else "Deployment was rejected and cannot be promoted."
                ),
                evidence=_deployment_evidence(record),
            )
        )
    return tuple(entries)


def _deployment_detail(record: DeploymentRecord) -> str:
    intent = record.intent
    parts = [
        f"Project: {intent.project_id}",
        f"Environment: {intent.environment.environment_id}",
        f"Tier: {intent.environment.tier.value}",
        f"Release SHA: {intent.release.source_sha}",
        f"State: {record.state.value}",
    ]
    if record.previous_release_sha is not None:
        parts.append(f"Previous release SHA: {record.previous_release_sha}")
    if record.health is not None:
        parts.append(f"Health: {'healthy' if record.health.healthy else 'unhealthy'}")
        parts.append(f"Health checked: {record.health.checked_at.isoformat()}")
    if record.rollback is not None:
        parts.append(f"Rollback: {'succeeded' if record.rollback.succeeded else 'failed'}")
        if record.rollback.restored_release_sha is not None:
            parts.append(f"Restored release SHA: {record.rollback.restored_release_sha}")
    return "; ".join(parts)


def _deployment_evidence(record: DeploymentRecord) -> tuple[EvidenceReference, ...]:
    items: list[EvidenceReference] = [
        EvidenceReference(
            kind="deployment",
            reference=reference,
            label="Deployment provider evidence",
        )
        for reference in record.provider_evidence_refs
    ]
    if record.health is not None:
        items.extend(
            EvidenceReference(
                kind="health",
                reference=reference,
                label="Deployment health evidence",
            )
            for reference in record.health.evidence_refs
        )
    if record.rollback is not None:
        items.extend(
            EvidenceReference(
                kind="rollback",
                reference=reference,
                label="Deployment rollback evidence",
            )
            for reference in record.rollback.evidence_refs
        )
    return tuple(items)
