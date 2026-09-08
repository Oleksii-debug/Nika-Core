from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductStatusEntry,
    ProductStatusKind,
)

_MAX_FIELD = 4000


class FactoryOperatorProjection(BaseModel):
    """Bounded read-only Product Factory status for an operator or downstream agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = Field(alias="PROJECT", min_length=1, max_length=160)
    work: str = Field(alias="WORK", min_length=1, max_length=_MAX_FIELD)
    owner: str = Field(alias="OWNER", min_length=1, max_length=_MAX_FIELD)
    state: str = Field(alias="STATE", min_length=1, max_length=80)
    blocker: str = Field(alias="BLOCKER", min_length=1, max_length=_MAX_FIELD)
    candidate: str = Field(alias="CANDIDATE", min_length=1, max_length=_MAX_FIELD)
    test: str = Field(alias="TEST", min_length=1, max_length=_MAX_FIELD)
    qa: str = Field(alias="QA", min_length=1, max_length=_MAX_FIELD)
    integration: str = Field(alias="INTEGRATION", min_length=1, max_length=_MAX_FIELD)
    next: str = Field(alias="NEXT", min_length=1, max_length=_MAX_FIELD)


def project_operator_status(detail: ProductProjectDetail) -> FactoryOperatorProjection:
    """Project existing ProductProject presentation state without inventing new authority."""

    component_entries = _statuses(detail, ProductStatusKind.COMPONENT)
    blocker_entries = _statuses(detail, ProductStatusKind.BLOCKER)
    qa_entries = _statuses(detail, ProductStatusKind.QA)
    integration_entries = tuple(
        entry
        for entry in detail.statuses
        if entry.kind in {ProductStatusKind.RELEASE, ProductStatusKind.DEPLOYMENT}
    )

    owners = tuple(
        dict.fromkeys(entry.owner for entry in detail.statuses if entry.owner is not None)
    )
    candidate_refs = tuple(
        dict.fromkeys(
            evidence.reference
            for entry in detail.statuses
            for evidence in entry.evidence
            if evidence.kind == "git_commit"
        )
    )

    return FactoryOperatorProjection(
        PROJECT=detail.summary.project_id,
        WORK=_render_statuses(component_entries, empty="none"),
        OWNER=_render_values(owners, empty="unassigned"),
        STATE=detail.summary.state,
        BLOCKER=_render_statuses(blocker_entries, empty="none"),
        CANDIDATE=_render_values(candidate_refs, empty="unknown"),
        TEST=_render_test_state(qa_entries),
        QA=_render_statuses(qa_entries, empty="unknown"),
        INTEGRATION=_render_statuses(integration_entries, empty="not_started"),
        NEXT=_next_action(detail, component_entries, blocker_entries),
    )


def _statuses(
    detail: ProductProjectDetail,
    kind: ProductStatusKind,
) -> tuple[ProductStatusEntry, ...]:
    return tuple(entry for entry in detail.statuses if entry.kind is kind)


def _render_statuses(
    entries: tuple[ProductStatusEntry, ...],
    *,
    empty: str,
) -> str:
    values = tuple(f"{entry.item_id}={entry.state}" for entry in entries)
    return _render_values(values, empty=empty)


def _render_values(values: tuple[str, ...], *, empty: str) -> str:
    if not values:
        return empty
    rendered = ", ".join(values)
    if len(rendered) <= _MAX_FIELD:
        return rendered
    return rendered[: _MAX_FIELD - 3].rstrip() + "..."


def _render_test_state(entries: tuple[ProductStatusEntry, ...]) -> str:
    if not entries:
        return "unknown"
    states = tuple(dict.fromkeys(entry.state for entry in entries))
    return _render_values(states, empty="unknown")


def _next_action(
    detail: ProductProjectDetail,
    component_entries: tuple[ProductStatusEntry, ...],
    blocker_entries: tuple[ProductStatusEntry, ...],
) -> str:
    if blocker_entries:
        return "resolve_blocker"
    if detail.summary.current_decision is not None:
        return f"owner_decision:{detail.summary.current_decision.decision_id}"
    active = tuple(
        entry
        for entry in component_entries
        if entry.state not in {"accepted", "done", "completed", "succeeded"}
    )
    if active:
        return f"continue_work:{active[0].item_id}"
    if component_entries:
        return "next_work"
    return "inspect_project"
