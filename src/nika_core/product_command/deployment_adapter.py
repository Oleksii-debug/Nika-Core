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
    ReleaseRef,
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
    """Project exact PF3 deployment state without exposing provider credentials."""
    _validate_snapshot_backing(snapshot)
    entries: list[ProductStatusEntry] = []
    for record in snapshot.records:
        entries.extend(_record_entries(record))
    return tuple(entries)


def _validate_snapshot_backing(snapshot: DeploymentFabricSnapshot) -> None:
    """Mirror PF3 restore integrity before projecting any deployment state."""
    intent_ids = [record.intent.intent_id for record in snapshot.records]
    if len(intent_ids) != len(set(intent_ids)):
        raise DeploymentPresentationIntegrityError(
            "deployment snapshot contains duplicate intent identities"
        )
    for record in snapshot.records:
        _validate_record_semantics(record)

    staging_projects: set[str] = set()
    for entry in snapshot.healthy_staging:
        project_id, release = _normalize_healthy_staging_entry(
            entry,
            snapshot.records,
        )
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
                release=release,
                require_staging=True,
            )
            for record in snapshot.records
        ):
            raise DeploymentPresentationIntegrityError(
                "healthy staging marker is not backed by a healthy exact deployment"
            )

    current_keys: set[tuple[str, str]] = set()
    for entry in snapshot.current_releases:
        project_id, environment_id, release = _normalize_current_release_entry(
            entry,
            snapshot.records,
        )
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
                release=release,
                require_staging=False,
            )
            for record in snapshot.records
        ):
            raise DeploymentPresentationIntegrityError(
                "current release marker is not backed by a healthy exact deployment record"
            )


def _normalize_healthy_staging_entry(
    entry: tuple[str, ...],
    records: tuple[DeploymentRecord, ...],
) -> tuple[str, ReleaseRef]:
    if len(entry) == 4:
        project_id, version, source_sha, artifact_digest = entry
        try:
            release = ReleaseRef(project_id, version, source_sha, artifact_digest)
        except ValueError as exc:
            raise DeploymentPresentationIntegrityError(
                "healthy staging marker contains invalid exact release identity"
            ) from exc
        return project_id, release
    if len(entry) != 2:
        raise DeploymentPresentationIntegrityError(
            "healthy staging marker must use exact release identity or unambiguous legacy SHA"
        )
    project_id, source_sha = entry
    if not project_id.strip() or not _is_sha(source_sha):
        raise DeploymentPresentationIntegrityError(
            "healthy staging marker identity/release SHA is invalid"
        )
    matches = {
        record.intent.release
        for record in records
        if record.state is DeploymentState.HEALTHY
        and record.intent.project_id == project_id
        and record.intent.environment.tier is EnvironmentTier.STAGING
        and record.intent.release.source_sha == source_sha
        and record.health is not None
        and record.health.healthy
        and record.health.release == record.intent.release
    }
    if len(matches) != 1:
        raise DeploymentPresentationIntegrityError(
            "legacy healthy staging marker is ambiguous or not backed by one exact release"
        )
    return project_id, next(iter(matches))


def _normalize_current_release_entry(
    entry: tuple[str, ...],
    records: tuple[DeploymentRecord, ...],
) -> tuple[str, str, ReleaseRef]:
    if len(entry) == 5:
        project_id, environment_id, version, source_sha, artifact_digest = entry
        if not project_id.strip() or not environment_id.strip():
            raise DeploymentPresentationIntegrityError(
                "current-release identity contains an empty field"
            )
        try:
            release = ReleaseRef(project_id, version, source_sha, artifact_digest)
        except ValueError as exc:
            raise DeploymentPresentationIntegrityError(
                "current-release identity contains invalid exact release data"
            ) from exc
        return project_id, environment_id, release

    if len(entry) == 3:
        project_id, environment_id, source_sha = entry
        if not all(value.strip() for value in (project_id, environment_id, source_sha)):
            raise DeploymentPresentationIntegrityError(
                "current-release identity contains an empty field"
            )
        if not _is_sha(source_sha):
            raise DeploymentPresentationIntegrityError(
                "current-release identity contains an invalid release SHA"
            )
        matches = {
            record.intent.release
            for record in records
            if record.state is DeploymentState.HEALTHY
            and record.intent.project_id == project_id
            and record.intent.environment.environment_id == environment_id
            and record.intent.release.source_sha == source_sha
            and record.health is not None
            and record.health.healthy
            and record.health.release == record.intent.release
        }
        if len(matches) != 1:
            raise DeploymentPresentationIntegrityError(
                "legacy current release is ambiguous or not backed by one exact release"
            )
        return project_id, environment_id, next(iter(matches))

    if len(entry) == 2:
        environment_id, source_sha = entry
        if not environment_id.strip() or not _is_sha(source_sha):
            raise DeploymentPresentationIntegrityError(
                "legacy current-release identity contains invalid environment/release SHA"
            )
        matches = {
            (record.intent.project_id, record.intent.release)
            for record in records
            if record.state is DeploymentState.HEALTHY
            and record.intent.environment.environment_id == environment_id
            and record.intent.release.source_sha == source_sha
            and record.health is not None
            and record.health.healthy
            and record.health.release == record.intent.release
        }
        if len(matches) != 1:
            raise DeploymentPresentationIntegrityError(
                "legacy current release is ambiguous or not backed by one healthy project"
            )
        project_id, release = next(iter(matches))
        return project_id, environment_id, release

    raise DeploymentPresentationIntegrityError(
        "current-release identity must contain exact project/environment/release data "
        "or an unambiguous legacy SHA shape"
    )


def _validate_record_semantics(record: DeploymentRecord) -> None:
    intent = record.intent
    if record.previous_release_sha is not None and not _is_sha(record.previous_release_sha):
        raise DeploymentPresentationIntegrityError(
            "deployment record contains an invalid previous release SHA"
        )
    if record.previous_release is not None:
        if (
            record.previous_release.project_id != intent.project_id
            or record.previous_release_sha != record.previous_release.source_sha
        ):
            raise DeploymentPresentationIntegrityError(
                "deployment previous release exact identity is inconsistent"
            )
    if not record.provider_evidence_refs and record.state is not DeploymentState.UNCERTAIN:
        raise DeploymentPresentationIntegrityError(
            "deployment terminal record requires provider evidence references"
        )
    if record.health is not None:
        if (
            record.health.environment_id != intent.environment.environment_id
            or record.health.release_sha != intent.release.source_sha
            or record.health.release != intent.release
        ):
            raise DeploymentPresentationIntegrityError(
                "deployment health evidence does not match exact intent release"
            )
    if record.rollback is not None:
        if (
            record.rollback.environment_id != intent.environment.environment_id
            or record.rollback.failed_release_sha != intent.release.source_sha
            or record.rollback.failed_release != intent.release
        ):
            raise DeploymentPresentationIntegrityError(
                "deployment rollback evidence does not match exact failed release"
            )
        if record.rollback.succeeded:
            if record.previous_release is None:
                if (
                    record.rollback.restored_release_sha is not None
                    or record.rollback.restored_release is not None
                ):
                    raise DeploymentPresentationIntegrityError(
                        "deployment rollback unexpectedly restored an unrecorded release"
                    )
            elif record.rollback.restored_release != record.previous_release:
                raise DeploymentPresentationIntegrityError(
                    "deployment rollback restored a release other than exact previous release"
                )

    if record.state is DeploymentState.HEALTHY:
        if record.health is None or not record.health.healthy or record.rollback is not None:
            raise DeploymentPresentationIntegrityError(
                "healthy deployment record is semantically inconsistent"
            )
    elif record.state is DeploymentState.ROLLED_BACK:
        if (
            record.health is None
            or record.health.healthy
            or record.rollback is None
            or not record.rollback.succeeded
        ):
            raise DeploymentPresentationIntegrityError(
                "rolled-back deployment record is semantically inconsistent"
            )
    elif record.state is DeploymentState.HEALTH_CHECK:
        raise DeploymentPresentationIntegrityError(
            "health-check deployment state must not be serialized as durable"
        )
    elif record.state is DeploymentState.UNCERTAIN:
        if record.health is not None and record.health.healthy:
            raise DeploymentPresentationIntegrityError(
                "uncertain deployment record cannot contain healthy terminal evidence"
            )
        if record.rollback is not None:
            raise DeploymentPresentationIntegrityError(
                "uncertain deployment record cannot contain terminal rollback evidence"
            )


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_healthy_record(
    record: DeploymentRecord,
    *,
    project_id: str,
    environment_id: str | None,
    release: ReleaseRef,
    require_staging: bool,
) -> bool:
    intent = record.intent
    if (
        record.state is not DeploymentState.HEALTHY
        or record.health is None
        or not record.health.healthy
        or record.health.release != intent.release
        or intent.project_id != project_id
        or intent.release != release
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
            f"Project: {release.project_id}; version: {release.version}; "
            f"source SHA: {release.source_sha}; artifact SHA-256: {release.artifact_digest}; "
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
        f"Release version: {intent.release.version}",
        f"Release SHA: {intent.release.source_sha}",
        f"Artifact SHA-256: {intent.release.artifact_digest}",
        f"State: {record.state.value}",
    ]
    if record.previous_release is not None:
        parts.append(f"Previous release version: {record.previous_release.version}")
        parts.append(f"Previous release SHA: {record.previous_release.source_sha}")
        parts.append(
            f"Previous artifact SHA-256: {record.previous_release.artifact_digest}"
        )
    elif record.previous_release_sha is not None:
        parts.append(f"Legacy previous release SHA: {record.previous_release_sha}")
    if record.health is not None:
        parts.append(f"Health: {'healthy' if record.health.healthy else 'unhealthy'}")
        parts.append(f"Health checked: {record.health.checked_at.isoformat()}")
    if record.rollback is not None:
        parts.append(f"Rollback: {'succeeded' if record.rollback.succeeded else 'failed'}")
        if record.rollback.restored_release is not None:
            parts.append(
                f"Restored release version: {record.rollback.restored_release.version}"
            )
            parts.append(
                f"Restored release SHA: {record.rollback.restored_release.source_sha}"
            )
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
