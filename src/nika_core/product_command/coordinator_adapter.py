from __future__ import annotations

from nika_core.product_command.contracts import (
    EvidenceReference,
    ProductStatusEntry,
    ProductStatusKind,
)
from nika_core.product_factory_coordinator import CoordinatorSnapshot, WorkRecord, WorkState

_STATE_LABELS = {
    WorkState.PLANNED: "Заплановано",
    WorkState.READY: "Готово до виконання",
    WorkState.RUNNING: "Виконується",
    WorkState.REVIEW_REQUIRED: "Очікує незалежної перевірки",
    WorkState.ACCEPTED: "Прийнято",
    WorkState.REPAIR_REQUIRED: "Потрібне виправлення",
    WorkState.BLOCKED: "Заблоковано",
}


def coordinator_status_entries(snapshot: CoordinatorSnapshot) -> tuple[ProductStatusEntry, ...]:
    """Project integrated PF2 state into stable PF5 textual presentation contracts."""
    entries: list[ProductStatusEntry] = []
    for record in snapshot.records:
        entries.append(_component_entry(record))
        if record.result is not None:
            entries.append(_qa_entry(record))
        if record.blocker:
            entries.append(_blocker_entry(record))
    return tuple(entries)


def _component_entry(record: WorkRecord) -> ProductStatusEntry:
    request = record.request
    detail_parts = [
        f"Стан: {_STATE_LABELS[record.state]}",
        f"Repository: {request.repository_id}",
        f"Base SHA: {request.base_sha}",
        f"Attempt: {request.attempt}",
        f"Allowed paths: {', '.join(request.allowed_paths)}",
    ]
    if record.review is not None:
        detail_parts.append(
            f"Independent review: {'accepted' if record.review.accepted else 'rejected'} "
            f"by {record.review.reviewer_id}"
        )
    return ProductStatusEntry(
        kind=ProductStatusKind.COMPONENT,
        item_id=request.component_id,
        label=f"Компонент {request.component_id}",
        state=record.state.value,
        detail="; ".join(detail_parts),
        evidence=_evidence(record),
    )


def _qa_entry(record: WorkRecord) -> ProductStatusEntry:
    result = record.result
    assert result is not None
    tests = result.coding_result.test_evidence
    passed = bool(tests) and all(item.exit_code == 0 for item in tests)
    return ProductStatusEntry(
        kind=ProductStatusKind.QA,
        item_id=f"{record.request.component_id}:qa",
        label=f"Перевірки {record.request.component_id}",
        state="passed" if passed else "failed",
        detail=f"Recorded test evidence: {len(tests)} command(s).",
        evidence=_evidence(record),
    )


def _blocker_entry(record: WorkRecord) -> ProductStatusEntry:
    assert record.blocker is not None
    return ProductStatusEntry(
        kind=ProductStatusKind.BLOCKER,
        item_id=f"{record.request.component_id}:blocker",
        label=f"Блокер {record.request.component_id}",
        state="active",
        detail=record.blocker,
        evidence=_evidence(record),
    )


def _evidence(record: WorkRecord) -> tuple[EvidenceReference, ...]:
    items: list[EvidenceReference] = []
    if record.result is not None:
        items.extend(
            (
                EvidenceReference(
                    kind="git_commit",
                    reference=record.result.result_sha,
                    label="Exact worker result SHA",
                ),
                EvidenceReference(
                    kind="diff_digest",
                    reference=f"sha256:{record.result.diff_digest}",
                    sha256=record.result.diff_digest,
                    label="Worker diff SHA-256",
                ),
            )
        )
    if record.review is not None:
        items.extend(
            EvidenceReference(
                kind="review",
                reference=reference,
                label="Independent review evidence",
            )
            for reference in record.review.evidence_refs
        )
    return tuple(items)
