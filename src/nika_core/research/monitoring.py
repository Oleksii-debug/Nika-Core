from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nika_core.research.models import (
    FreshnessState,
    HttpSourceState,
    ResearchEvidence,
    ResearchResultSet,
    SourceKind,
)
from nika_core.research.network_repository import NetworkResearchRepository


class SourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    REMOVED = "removed"
    UNKNOWN = "unknown"


class ChangeDetectionStatus(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    CONDITION_MATCHED = "condition_matched"
    ERROR = "error"
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
class NormalizedObservation:
    """Thin V0.1 projection over already-normalized, durable Research content.

    ``content_id`` must identify the normalized content, for example the existing
    persisted ``document_id`` produced by Research ingestion. Observation time,
    locator changes, and other evidence metadata are intentionally not part of
    change identity.
    """

    source_id: str
    source_kind: SourceKind
    content_id: str | None
    evidence: tuple[ResearchEvidence, ...] = ()
    condition_matched: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeDetectionResult:
    source_id: str
    source_kind: SourceKind
    status: ChangeDetectionStatus
    changed: bool | None
    condition_matched: bool
    previous_content_id: str | None
    current_content_id: str | None
    previous_evidence: tuple[ResearchEvidence, ...]
    current_evidence: tuple[ResearchEvidence, ...]
    error_code: str | None = None
    error_message: str | None = None


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


def _validate_observation(observation: NormalizedObservation, label: str) -> None:
    if not isinstance(observation.source_id, str) or not observation.source_id.strip():
        raise ValueError(f"{label} observation source_id is required")
    if observation.source_id != observation.source_id.strip():
        raise ValueError(f"{label} observation source_id must be normalized")
    if not isinstance(observation.source_kind, SourceKind):
        raise TypeError(f"{label} observation source_kind is invalid")
    if observation.content_id is not None:
        if not isinstance(observation.content_id, str) or not observation.content_id.strip():
            raise ValueError(f"{label} observation content_id must be non-empty or None")
        if observation.content_id != observation.content_id.strip():
            raise ValueError(f"{label} observation content_id must be normalized")
    if not isinstance(observation.condition_matched, bool):
        raise TypeError(f"{label} observation condition_matched must be boolean")
    if observation.error_code is not None:
        if not isinstance(observation.error_code, str) or not observation.error_code.strip():
            raise ValueError(f"{label} observation error_code must be non-empty or None")
        if observation.error_code != observation.error_code.strip():
            raise ValueError(f"{label} observation error_code must be normalized")
    if observation.error_message is not None and not isinstance(observation.error_message, str):
        raise ValueError(f"{label} observation error_message must be text or None")
    if observation.condition_matched and observation.content_id is None:
        raise ValueError(f"{label} observation condition cannot match without content")
    if observation.condition_matched and observation.error_code is not None:
        raise ValueError(f"{label} observation cannot be both matched and errored")
    for evidence in observation.evidence:
        if not isinstance(evidence, ResearchEvidence):
            raise TypeError(f"{label} observation evidence item is invalid")
        if evidence.source_id != observation.source_id:
            raise ValueError(f"{label} observation evidence source_id mismatch")
        if evidence.source_kind is not observation.source_kind:
            raise ValueError(f"{label} observation evidence source_kind mismatch")


def detect_observation_change(
    previous: NormalizedObservation | None,
    current: NormalizedObservation,
) -> ChangeDetectionResult:
    """Classify one normalized source observation without fuzzy or temporal heuristics.

    The detector is deliberately pure. Durable snapshot ownership stays with the
    existing Research repositories/recurring-run lineage, so process restart does
    not require a second persistence mechanism here.
    """
    _validate_observation(current, "current")
    if previous is not None:
        _validate_observation(previous, "previous")
        if (
            previous.source_id != current.source_id
            or previous.source_kind is not current.source_kind
        ):
            raise ValueError("observations must belong to the same stable source")

    changed: bool | None = None
    if (
        current.error_code is None
        and previous is not None
        and previous.content_id is not None
        and previous.error_code is None
        and current.content_id is not None
    ):
        changed = previous.content_id != current.content_id

    if current.error_code is not None:
        status = ChangeDetectionStatus.ERROR
    elif current.condition_matched:
        status = ChangeDetectionStatus.CONDITION_MATCHED
    elif (
        current.content_id is None
        or previous is None
        or previous.error_code is not None
        or previous.content_id is None
    ):
        status = ChangeDetectionStatus.UNKNOWN
    elif changed:
        status = ChangeDetectionStatus.CHANGED
    else:
        status = ChangeDetectionStatus.UNCHANGED

    return ChangeDetectionResult(
        source_id=current.source_id,
        source_kind=current.source_kind,
        status=status,
        changed=changed,
        condition_matched=current.condition_matched,
        previous_content_id=previous.content_id if previous is not None else None,
        current_content_id=current.content_id,
        previous_evidence=previous.evidence if previous is not None else (),
        current_evidence=current.evidence,
        error_code=current.error_code,
        error_message=current.error_message,
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
