from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from nika_core.product_command.contracts import (
    ProductProjectDetail,
    ProductStatusEntry,
    ProductStatusKind,
)

_MAX_FIELD = 4000
_TERMINAL_SUCCESS_STATES_BY_KIND: dict[ProductStatusKind, frozenset[str]] = {
    ProductStatusKind.BLOCKER: frozenset({"completed", "done", "resolved"}),
    ProductStatusKind.COMPONENT: frozenset({"accepted", "completed", "done"}),
    ProductStatusKind.BUILD: frozenset({"pass", "passed", "succeeded", "success"}),
    ProductStatusKind.QA: frozenset({"pass", "passed"}),
    ProductStatusKind.DEPLOYMENT: frozenset({"deployed", "healthy", "succeeded"}),
    ProductStatusKind.RELEASE: frozenset({"released"}),
}
_CANDIDATE_STATUS_KINDS = frozenset(
    {
        ProductStatusKind.COMPONENT,
        ProductStatusKind.BUILD,
        ProductStatusKind.QA,
        ProductStatusKind.DEPLOYMENT,
        ProductStatusKind.RELEASE,
    }
)
_TERMINAL_CANDIDATE_STATUS_KINDS = frozenset(
    {
        ProductStatusKind.BUILD,
        ProductStatusKind.QA,
        ProductStatusKind.DEPLOYMENT,
        ProductStatusKind.RELEASE,
    }
)


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
    active_blocker_entries = _incomplete(blocker_entries)
    build_entries = _statuses(detail, ProductStatusKind.BUILD)
    qa_entries = _statuses(detail, ProductStatusKind.QA)
    integration_entries = tuple(
        entry
        for entry in detail.statuses
        if entry.kind in {ProductStatusKind.RELEASE, ProductStatusKind.DEPLOYMENT}
    )

    owners = tuple(
        dict.fromkeys(
            entry.owner.strip()
            for entry in detail.statuses
            if entry.owner is not None and entry.owner.strip()
        )
    )
    candidate_refs = tuple(
        dict.fromkeys(
            evidence.reference
            for entry in _active_candidate_entries(detail)
            for evidence in entry.evidence
            if evidence.kind == "git_commit"
        )
    )

    return FactoryOperatorProjection(
        PROJECT=detail.summary.project_id,
        WORK=_render_work(detail, component_entries),
        OWNER=_render_owner(owners),
        STATE=detail.summary.state,
        BLOCKER=_render_blockers(active_blocker_entries, detail.summary.blocker_count),
        CANDIDATE=_render_candidate(candidate_refs),
        TEST=_render_statuses(build_entries, empty="unknown"),
        QA=_render_statuses(qa_entries, empty="unknown"),
        INTEGRATION=_render_statuses(integration_entries, empty="not_started"),
        NEXT=_next_action(
            detail,
            component_entries,
            blocker_entries,
            build_entries,
            qa_entries,
            integration_entries,
        ),
    )


def _statuses(
    detail: ProductProjectDetail,
    kind: ProductStatusKind,
) -> tuple[ProductStatusEntry, ...]:
    return tuple(entry for entry in detail.statuses if entry.kind is kind)


def _render_work(
    detail: ProductProjectDetail,
    component_entries: tuple[ProductStatusEntry, ...],
) -> str:
    if component_entries:
        return _render_statuses(component_entries, empty="none")
    return detail.summary.goal


def _render_statuses(
    entries: tuple[ProductStatusEntry, ...],
    *,
    empty: str,
) -> str:
    values = tuple(f"{entry.item_id}={entry.state}" for entry in entries)
    return _render_values(values, empty=empty)


def _render_blockers(
    active_entries: tuple[ProductStatusEntry, ...],
    reported_count: int,
) -> str:
    values = [f"{entry.item_id}={entry.state}" for entry in active_entries]
    unrepresented = max(0, reported_count - len(active_entries))
    if unrepresented:
        values.append(f"unrepresented_blockers={unrepresented}")
    return _render_values(tuple(values), empty="none")


def _render_values(values: tuple[str, ...], *, empty: str) -> str:
    if not values:
        return empty
    rendered = ", ".join(values)
    if len(rendered) <= _MAX_FIELD:
        return rendered
    return rendered[: _MAX_FIELD - 3].rstrip() + "..."


def _render_owner(owners: tuple[str, ...]) -> str:
    if not owners:
        return "unassigned"
    if len(owners) > 1:
        return "ambiguous_multiple_owners"
    return owners[0]


def _render_candidate(candidate_refs: tuple[str, ...]) -> str:
    if not candidate_refs:
        return "unknown"
    if any(re.fullmatch(r"[0-9a-f]{40}", reference) is None for reference in candidate_refs):
        return "invalid_candidate_identity"
    if len(candidate_refs) > 1:
        return "ambiguous_multiple_candidates"
    return candidate_refs[0]


def _normalized_state(entry: ProductStatusEntry) -> str:
    return entry.state.strip().casefold()


def _is_terminal_success(entry: ProductStatusEntry) -> bool:
    allowed = _TERMINAL_SUCCESS_STATES_BY_KIND.get(entry.kind)
    return allowed is not None and _normalized_state(entry) in allowed


def _incomplete(
    entries: tuple[ProductStatusEntry, ...],
) -> tuple[ProductStatusEntry, ...]:
    return tuple(entry for entry in entries if not _is_terminal_success(entry))


def _active_candidate_entries(detail: ProductProjectDetail) -> tuple[ProductStatusEntry, ...]:
    candidate_entries = tuple(
        entry for entry in detail.statuses if entry.kind in _CANDIDATE_STATUS_KINDS
    )
    active_entries = _incomplete(candidate_entries)
    if active_entries:
        return active_entries
    return tuple(
        entry
        for entry in candidate_entries
        if entry.kind in _TERMINAL_CANDIDATE_STATUS_KINDS
        and _is_terminal_success(entry)
    )


def _first_incomplete(
    entries: tuple[ProductStatusEntry, ...],
) -> ProductStatusEntry | None:
    return next(iter(_incomplete(entries)), None)


def _next_action(
    detail: ProductProjectDetail,
    component_entries: tuple[ProductStatusEntry, ...],
    blocker_entries: tuple[ProductStatusEntry, ...],
    build_entries: tuple[ProductStatusEntry, ...],
    qa_entries: tuple[ProductStatusEntry, ...],
    integration_entries: tuple[ProductStatusEntry, ...],
) -> str:
    if _first_incomplete(blocker_entries) is not None or detail.summary.blocker_count > 0:
        return "resolve_blocker"
    if (
        detail.summary.current_decision is not None
        and detail.summary.current_decision.state == "pending"
    ):
        return f"owner_decision:{detail.summary.current_decision.decision_id}"
    active = _first_incomplete(component_entries)
    if active is not None:
        return f"continue_work:{active.item_id}"
    build = _first_incomplete(build_entries)
    if build is not None:
        return f"test:{build.item_id}={build.state}"
    if component_entries and not build_entries and not qa_entries:
        return "test:not_started"
    qa = _first_incomplete(qa_entries)
    if qa is not None:
        return f"qa:{qa.item_id}={qa.state}"
    if build_entries and not qa_entries:
        return "qa:not_started"
    integration = _first_incomplete(integration_entries)
    if integration is not None:
        return f"integration:{integration.item_id}={integration.state}"
    if qa_entries and not integration_entries:
        return "integration:not_started"
    if component_entries:
        return "next_work"
    return "inspect_project"
