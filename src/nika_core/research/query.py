from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    FreshnessState,
    ResearchResultSet,
    SearchHit,
    SourceKind,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.normalize import normalize_text
from nika_core.research.query_results import ScopedResearchResultWriter
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

RESEARCH_SEARCH_ACTION_CLASS = "research.search"


def research_workspace_target(workspace_id: str) -> str:
    return f"research-workspace:{workspace_id}"


def research_document_resource(document_id: str) -> str:
    return f"research-document:{document_id}"


class SearchMode(StrEnum):
    LITERAL = "literal"
    PHRASE = "phrase"


@dataclass(frozen=True, slots=True)
class ResearchSearchFilters:
    source_ids: tuple[str, ...] = ()
    source_kinds: tuple[SourceKind, ...] = ()
    media_types: tuple[str, ...] = ()
    freshness: tuple[FreshnessState, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchQuerySpec:
    workspace_id: str
    text: str
    mode: SearchMode = SearchMode.LITERAL
    filters: ResearchSearchFilters = field(default_factory=ResearchSearchFilters)
    limit: int = 20


@dataclass(frozen=True, slots=True)
class ResearchQueryAuthorization:
    """Trusted current-context authority for one retrieval execution."""

    permission_id: str
    subject_id: str
    context: PermissionContext

    def __post_init__(self) -> None:
        if not self.permission_id.strip():
            raise ValueError("permission_id is required")
        if not self.subject_id.strip():
            raise ValueError("subject_id is required")


@dataclass(frozen=True, slots=True)
class ResearchQueryExecution:
    spec: ResearchQuerySpec
    result_set: ResearchResultSet


class DeterministicResearchQueryService:
    """Safe FTS5 query/filter layer with optional canonical read authorization."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        network_repository: NetworkResearchRepository,
        permission_store: StandingPermissionStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._network = network_repository
        self._permissions = permission_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._result_writer = ScopedResearchResultWriter(
            store=store,
            network_repository=network_repository,
        )

    def execute(
        self,
        spec: ResearchQuerySpec,
        *,
        authorization: ResearchQueryAuthorization | None = None,
        result_set_id: str | None = None,
    ) -> ResearchQueryExecution:
        self._validate_spec(spec)
        if self._permissions is None:
            if authorization is not None:
                raise ValueError("authorization requires a configured permission store")
        elif authorization is None:
            raise PermissionError("research retrieval requires current authorization context")

        hits = self._search(
            spec,
            self._fts_query(spec.text, spec.mode),
            authorization=authorization,
        )
        match_reason = (
            f"Quoted phrase full-text match for: {spec.text}"
            if spec.mode is SearchMode.PHRASE
            else f"Literal-token full-text match for: {spec.text}"
        )
        result_set = self._result_writer.save(
            workspace_id=spec.workspace_id,
            query=spec.text,
            hits=hits,
            source_ids=self._normalized_source_ids(spec.filters.source_ids),
            source_kinds=spec.filters.source_kinds,
            freshness=spec.filters.freshness,
            why_matched=match_reason,
            result_set_id=result_set_id,
        )
        return ResearchQueryExecution(spec=spec, result_set=result_set)

    @staticmethod
    def render_text(execution: ResearchQueryExecution) -> str:
        spec = execution.spec
        filters = spec.filters
        lines = ["Research results", f"Query: {spec.text}", f"Mode: {spec.mode.value}"]
        if filters.source_ids:
            lines.append(f"Source IDs: {', '.join(filters.source_ids)}")
        if filters.source_kinds:
            values = ", ".join(kind.value for kind in filters.source_kinds)
            lines.append(f"Source kinds: {values}")
        if filters.media_types:
            lines.append(f"Media types: {', '.join(filters.media_types)}")
        if filters.freshness:
            values = ", ".join(state.value for state in filters.freshness)
            lines.append(f"Freshness: {values}")
        lines.extend((f"Results: {len(execution.result_set.items)}", ""))

        for index, item in enumerate(execution.result_set.items, start=1):
            lines.extend(
                (f"{index}. {item.title}", f"Snippet: {item.snippet}", "Sources:")
            )
            if not item.evidence:
                lines.append("- No source provenance recorded")
            for evidence in item.evidence:
                label = evidence.source_kind.value
                if evidence.freshness is not None:
                    label += f", freshness={evidence.freshness.value}"
                lines.append(
                    f"- {label}: {evidence.locator} (observed {evidence.observed_at})"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _validate_spec(self, spec: ResearchQuerySpec) -> None:
        if not spec.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if spec.limit < 1 or spec.limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if (
            spec.filters.freshness
            and spec.filters.source_kinds
            and SourceKind.HTTP not in spec.filters.source_kinds
        ):
            raise ValueError("freshness filters require HTTP sources")

        source_ids = self._normalized_source_ids(spec.filters.source_ids)
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        with self._store.connection() as conn:
            local_rows = conn.execute(
                "SELECT source_id, workspace_id FROM research_sources "
                f"WHERE source_id IN ({placeholders})",
                source_ids,
            ).fetchall()
            http_rows = conn.execute(
                "SELECT source_id, workspace_id FROM research_http_sources "
                f"WHERE source_id IN ({placeholders})",
                source_ids,
            ).fetchall()
        owners: dict[str, set[str]] = {}
        for row in (*local_rows, *http_rows):
            owners.setdefault(row["source_id"], set()).add(row["workspace_id"])
        unknown = tuple(source_id for source_id in source_ids if source_id not in owners)
        if unknown:
            raise ValueError(f"unknown source_ids: {', '.join(unknown)}")
        if any(owners[source_id] != {spec.workspace_id} for source_id in source_ids):
            raise ValueError("source filter crosses workspace boundary")

    @staticmethod
    def _normalized_source_ids(source_ids: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(source_id.strip() for source_id in source_ids))
        if any(not source_id for source_id in normalized):
            raise ValueError("source_ids must not contain empty values")
        return normalized

    @staticmethod
    def _fts_query(text: str, mode: SearchMode) -> str:
        normalized = normalize_text(text).replace("\n", " ").strip()
        if not normalized:
            raise ValueError("search query must not be empty")
        if mode is SearchMode.PHRASE:
            return '"' + normalized.replace('"', '""') + '"'
        terms = [term for term in normalized.split(" ") if term]
        return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

    def _authorized_document_ids(
        self,
        *,
        spec: ResearchQuerySpec,
        candidates: tuple[str, ...],
        authorization: ResearchQueryAuthorization,
    ) -> tuple[str, ...]:
        assert self._permissions is not None
        authorized: list[str] = []
        for document_id in candidates:
            resource_id = research_document_resource(document_id)
            use = StandingPermissionUse(
                subject_id=authorization.subject_id,
                context=authorization.context,
                intent=ActionIntent(
                    action_id=f"research-search:{document_id}",
                    tool_id=RESEARCH_SEARCH_ACTION_CLASS,
                    risk=ToolRisk.READ_ONLY,
                    target=research_workspace_target(spec.workspace_id),
                    task_id=authorization.context.task_id,
                    project_id=authorization.context.project_id,
                    resource=resource_id,
                ),
                resource_id=resource_id,
            )
            try:
                self._permissions.authorize(
                    authorization.permission_id,
                    use,
                    now=self._clock(),
                )
            except PermissionError:
                continue
            authorized.append(document_id)
            if len(authorized) >= spec.limit:
                break
        return tuple(authorized)

    def _search(
        self,
        spec: ResearchQuerySpec,
        fts_query: str,
        *,
        authorization: ResearchQueryAuthorization | None,
    ) -> list[SearchHit]:
        filters = spec.filters
        clauses = ["corpus_fts MATCH ?", "corpus_fts.workspace_id=?"]
        params: list[object] = [fts_query, spec.workspace_id]

        if filters.media_types:
            media_types = tuple(
                dict.fromkeys(value.strip() for value in filters.media_types)
            )
            if any(not value for value in media_types):
                raise ValueError("media_types must not contain empty values")
            placeholders = ",".join("?" for _ in media_types)
            clauses.append(f"d.media_type IN ({placeholders})")
            params.extend(media_types)

        source_ids = self._normalized_source_ids(filters.source_ids)
        kinds = set(filters.source_kinds)
        freshness = tuple(dict.fromkeys(state.value for state in filters.freshness))
        origin_clauses: list[str] = []
        origin_params: list[object] = []
        allow_local = not kinds or SourceKind.LOCAL_FILE in kinds
        allow_http = not kinds or SourceKind.HTTP in kinds

        if (source_ids or kinds) and allow_local and not freshness:
            local = "SELECT 1 FROM corpus_origins lo WHERE lo.document_id=d.document_id"
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                local += f" AND lo.source_id IN ({placeholders})"
                origin_params.extend(source_ids)
            origin_clauses.append(f"EXISTS ({local})")

        if allow_http and (source_ids or kinds or freshness):
            http = (
                "SELECT 1 FROM corpus_http_origins ho "
                "JOIN research_http_sources hs ON hs.source_id=ho.source_id "
                "JOIN research_http_snapshots snap ON snap.snapshot_id=ho.snapshot_id "
                "WHERE ho.document_id=d.document_id "
                "AND hs.current_raw_sha256 IS NOT NULL "
                "AND snap.raw_sha256=hs.current_raw_sha256"
            )
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                http += f" AND ho.source_id IN ({placeholders})"
                origin_params.extend(source_ids)
            if freshness:
                placeholders = ",".join("?" for _ in freshness)
                http += f" AND hs.freshness IN ({placeholders})"
                origin_params.extend(freshness)
            origin_clauses.append(f"EXISTS ({http})")

        if source_ids or kinds or freshness:
            if not origin_clauses:
                return []
            clauses.append("(" + " OR ".join(origin_clauses) + ")")
            params.extend(origin_params)

        if self._permissions is not None:
            assert authorization is not None
            candidate_sql = f"""SELECT corpus_fts.document_id
            FROM corpus_fts
            JOIN corpus_documents d ON d.document_id=corpus_fts.document_id
            WHERE {' AND '.join(clauses)}
            ORDER BY bm25(corpus_fts), corpus_fts.document_id"""
            with self._store.connection() as conn:
                candidate_rows = conn.execute(candidate_sql, params).fetchall()
            authorized_ids = self._authorized_document_ids(
                spec=spec,
                candidates=tuple(row["document_id"] for row in candidate_rows),
                authorization=authorization,
            )
            if not authorized_ids:
                return []
            placeholders = ",".join("?" for _ in authorized_ids)
            clauses.append(f"corpus_fts.document_id IN ({placeholders})")
            params.extend(authorized_ids)

        params.append(spec.limit)
        sql = f"""SELECT corpus_fts.document_id, corpus_fts.title,
            snippet(corpus_fts, 3, '[', ']', ' … ', 24) AS snippet,
            bm25(corpus_fts) AS rank
        FROM corpus_fts
        JOIN corpus_documents d ON d.document_id=corpus_fts.document_id
        WHERE {' AND '.join(clauses)}
        ORDER BY rank, corpus_fts.document_id
        LIMIT ?"""
        with self._store.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SearchHit(
                document_id=row["document_id"],
                title=row["title"],
                snippet=row["snippet"],
                rank=float(row["rank"]),
            )
            for row in rows
        ]
