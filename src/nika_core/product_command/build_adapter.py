from __future__ import annotations

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_factory_build_execution import BuildExecutionState
from nika_core.product_factory_build_execution_persistence import (
    DurableBuildExecutionSnapshot,
)

_BLOCKING_STATES = frozenset(
    {
        BuildExecutionState.WAITING_FOR_NODE,
        BuildExecutionState.WAITING_FOR_AUTHORITY,
        BuildExecutionState.RECONCILE_REQUIRED,
        BuildExecutionState.FAILED,
    }
)

_STATE_LABELS = {
    BuildExecutionState.PENDING: "очікує підготовки",
    BuildExecutionState.WAITING_FOR_NODE: "очікує доступного build-вузла",
    BuildExecutionState.WAITING_FOR_AUTHORITY: "очікує чинної build-authority",
    BuildExecutionState.PREPARED: "підготовлено до build",
    BuildExecutionState.DISPATCHING: "передається build-вузлу",
    BuildExecutionState.EFFECT_IN_FLIGHT: "build-effect виконується",
    BuildExecutionState.RECONCILE_REQUIRED: "потрібне відновлення результату build",
    BuildExecutionState.SUCCEEDED: "build успішний",
    BuildExecutionState.FAILED: "build завершився невдало",
}

_BLOCKER_DEFAULTS = {
    BuildExecutionState.WAITING_FOR_NODE: "Немає доступного авторизованого build-вузла.",
    BuildExecutionState.WAITING_FOR_AUTHORITY: "Чинна build-authority більше не відповідає роботі.",
    BuildExecutionState.RECONCILE_REQUIRED: (
        "Результат build-effect невизначений; потрібне безпечне відновлення без повтору effect."
    ),
    BuildExecutionState.FAILED: "PF5 build завершився невдало.",
}


class BuildStatusProjectionError(ValueError):
    """PF5 durable build state cannot be projected safely."""


def build_status_entries(
    project_id: str,
    snapshot: DurableBuildExecutionSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    """Project one canonical PF5 durable checkpoint without exposing raw process data."""

    if type(project_id) is not str or not project_id or project_id != project_id.strip():
        raise BuildStatusProjectionError("project_id must be normalized non-empty text")
    if type(snapshot) is not DurableBuildExecutionSnapshot:
        raise BuildStatusProjectionError(
            "build status requires exact DurableBuildExecutionSnapshot"
        )

    records = snapshot.coordinator.records
    work_ids = [record.spec.request.work_id for record in records]
    if len(work_ids) != len(set(work_ids)):
        raise BuildStatusProjectionError("PF5 build status contains duplicate work identities")

    by_work = {record.spec.request.work_id: record for record in records}
    for record in records:
        request = record.spec.request
        grant = record.grant
        if (
            request.project_id != project_id
            or grant.project_id != project_id
            or grant.work_id != request.work_id
            or grant.repository_id != record.spec.scope.repository_id
        ):
            raise BuildStatusProjectionError(
                "PF5 build status contains cross-project or mismatched authority"
            )
        if len(request.work_id) > 140:
            raise BuildStatusProjectionError("PF5 build work identity is too long to present")
        if record.dispatch is not None and (
            record.dispatch.project_id != project_id
            or record.dispatch.work_id != request.work_id
            or record.dispatch.source_sha != record.spec.source_sha
            or record.dispatch.grant != grant
        ):
            raise BuildStatusProjectionError(
                "PF5 build dispatch does not match durable work authority"
            )
        if record.evidence is not None and (
            record.evidence.work_id != request.work_id
            or record.evidence.release_sha != record.spec.source_sha
            or record.node_id is None
            or record.evidence.node_id != record.node_id
        ):
            raise BuildStatusProjectionError(
                "PF5 build evidence does not match durable work identity"
            )
        if record.state is BuildExecutionState.SUCCEEDED and (
            record.evidence is None or not record.evidence.succeeded
        ):
            raise BuildStatusProjectionError(
                "successful PF5 build lacks matching success evidence"
            )
        if record.state is BuildExecutionState.FAILED and (
            record.evidence is not None and record.evidence.succeeded
        ):
            raise BuildStatusProjectionError(
                "failed PF5 build carries contradictory success evidence"
            )

    lease_ids: set[str] = set()
    for lease in snapshot.leases:
        if (
            lease.project_id != project_id
            or lease.work_id not in by_work
            or lease.lease_id in lease_ids
        ):
            raise BuildStatusProjectionError(
                "PF5 build lease set is outside the exact project/work identity"
            )
        record = by_work[lease.work_id]
        if record.lease_id != lease.lease_id or record.node_id != lease.node_id:
            raise BuildStatusProjectionError(
                "PF5 build lease does not match its durable work record"
            )
        lease_ids.add(lease.lease_id)

    evidence_work_ids: set[str] = set()
    for file_evidence in snapshot.file_evidence:
        if (
            file_evidence.project_id != project_id
            or file_evidence.work_id not in by_work
            or file_evidence.work_id in evidence_work_ids
        ):
            raise BuildStatusProjectionError(
                "PF5 file evidence is outside the exact project/work identity"
            )
        record = by_work[file_evidence.work_id]
        if (
            file_evidence.repository_id != record.grant.repository_id
            or file_evidence.source_sha != record.spec.source_sha
            or file_evidence.platform is not record.spec.request.platform
        ):
            raise BuildStatusProjectionError(
                "PF5 file evidence does not match its durable build work"
            )
        evidence_work_ids.add(file_evidence.work_id)

    entries: list[ProductStatusEntry] = []
    for record in records:
        entries.append(_build_entry(record))
        if record.state in _BLOCKING_STATES:
            entries.append(_blocker_entry(record))
    return tuple(entries)


def _build_entry(record) -> ProductStatusEntry:
    request = record.spec.request
    evidence = [
        EvidenceReference(
            kind="git_commit",
            reference=record.spec.source_sha,
            label="PF5 build source SHA",
        )
    ]
    if record.evidence is not None:
        evidence.append(
            EvidenceReference(
                kind="artifact_digest",
                reference=f"sha256:{record.evidence.artifact_digest}",
                sha256=record.evidence.artifact_digest,
                label="PF5 build artifact SHA-256",
            )
        )

    detail = [
        f"Стан: {_STATE_LABELS[record.state]}",
        f"Repository: {record.grant.repository_id}",
        f"Source SHA: {record.spec.source_sha}",
        f"Platform: {request.platform.value}",
        f"Attempt: {record.attempt}",
    ]
    if record.node_id is not None:
        detail.append(f"Build node: {record.node_id}")
    return ProductStatusEntry(
        kind=ProductStatusKind.BUILD,
        item_id=f"pf5-build:{request.work_id}",
        label=f"PF5 build {record.grant.repository_id}",
        state=record.state.value,
        detail="; ".join(detail),
        evidence=tuple(evidence),
    )


def _blocker_entry(record) -> ProductStatusEntry:
    request = record.spec.request
    reason = record.block_reason or _BLOCKER_DEFAULTS[record.state]
    if len(reason) > 4000:
        raise BuildStatusProjectionError("PF5 build blocker detail is too long to present")
    return ProductStatusEntry(
        kind=ProductStatusKind.BLOCKER,
        item_id=f"pf5-build:{request.work_id}:blocker",
        label=f"Блокер PF5 build {record.grant.repository_id}",
        state="active",
        detail=reason,
    )
