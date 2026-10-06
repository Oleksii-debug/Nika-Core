from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nika_core.research.models import FreshnessState, HttpSourceState, ResearchResultSet
from nika_core.research.network_repository import NetworkResearchRepository


class SourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    REMOVED = "removed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    status: SourceHealthStatus
    freshness: FreshnessState
    last_status_code: int | None
    last_error_code: str | None
    last_error_message: str | None
    last_attempt_at: str | None
    last_success_at: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceHealthReport:
    workspace_id: str
    sources: tuple[SourceHealth, ...]
    healthy: int
    degraded: int
    blocked: int
    removed: int
    unknown: int


@dataclass(frozen=True, slots=True)
class ResultDelta:
    previous_result_set_id: str
    current_result_set_id: str
    added_document_ids: tuple[str, ...]
    removed_document_ids: tuple[str, ...]
    retained_document_ids: tuple[str, ...]
    rank_changed_document_ids: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(
            self.added_document_ids
            or self.removed_document_ids
            or self.rank_changed_document_ids
        )


def _classify_source(source: HttpSourceState) -> SourceHealthStatus:
    if source.freshness is FreshnessState.CURRENT:
        return SourceHealthStatus.HEALTHY
    if source.freshness is FreshnessState.BLOCKED:
        return SourceHealthStatus.BLOCKED
    if source.freshness is FreshnessState.REMOVED:
        return SourceHealthStatus.REMOVED
    if source.freshness in {FreshnessState.STALE, FreshnessState.ERROR}:
        return SourceHealthStatus.DEGRADED
    return SourceHealthStatus.UNKNOWN


def build_workspace_health_report(
    repository: NetworkResearchRepository,
    workspace_id: str,
) -> WorkspaceHealthReport:
    """Build a deterministic report from persisted HTTP source state only.

    This performs no network access and does not infer health from wall-clock age.
    Freshness is owned by the refresh engine and remains restart-stable in SQLite.
    """
    sources = tuple(
        SourceHealth(
            source_id=source.source_id,
            status=_classify_source(source),
            freshness=source.freshness,
            last_status_code=source.last_status_code,
            last_error_code=source.last_error_code,
            last_error_message=source.last_error_message,
            last_attempt_at=source.last_attempt_at,
            last_success_at=source.last_success_at,
        )
        for source in repository.list_sources(workspace_id)
    )
    counts = {status: 0 for status in SourceHealthStatus}
    for source in sources:
        counts[source.status] += 1
    return WorkspaceHealthReport(
        workspace_id=workspace_id,
        sources=sources,
        healthy=counts[SourceHealthStatus.HEALTHY],
        degraded=counts[SourceHealthStatus.DEGRADED],
        blocked=counts[SourceHealthStatus.BLOCKED],
        removed=counts[SourceHealthStatus.REMOVED],
        unknown=counts[SourceHealthStatus.UNKNOWN],
    )


def diff_result_sets(
    previous: ResearchResultSet,
    current: ResearchResultSet,
) -> ResultDelta:
    """Compare two deterministic result sets without fuzzy identity heuristics."""
    if previous.workspace_id != current.workspace_id:
        raise ValueError("result sets must belong to the same workspace")
    previous_by_id = {item.document_id: item for item in previous.items}
    current_by_id = {item.document_id: item for item in current.items}
    previous_ids = set(previous_by_id)
    current_ids = set(current_by_id)
    retained = previous_ids & current_ids
    return ResultDelta(
        previous_result_set_id=previous.result_set_id,
        current_result_set_id=current.result_set_id,
        added_document_ids=tuple(sorted(current_ids - previous_ids)),
        removed_document_ids=tuple(sorted(previous_ids - current_ids)),
        retained_document_ids=tuple(sorted(retained)),
        rank_changed_document_ids=tuple(
            sorted(
                document_id
                for document_id in retained
                if previous_by_id[document_id].rank != current_by_id[document_id].rank
            )
        ),
    )
