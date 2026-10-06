from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SearchHit,
    SourceKind,
)
from nika_core.research.network_repository import NetworkResearchRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _evidence_json(evidence: tuple[ResearchEvidence, ...]) -> str:
    return json.dumps(
        [
            {
                "source_id": item.source_id,
                "source_kind": item.source_kind.value,
                "locator": item.locator,
                "observed_at": item.observed_at,
                "freshness": item.freshness.value if item.freshness is not None else None,
            }
            for item in evidence
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


class ScopedResearchResultWriter:
    """Persist a result set and its already-scoped provenance in one transaction."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        network_repository: NetworkResearchRepository,
    ) -> None:
        self._store = store
        self._network = network_repository

    def save(
        self,
        *,
        workspace_id: str,
        query: str,
        hits: list[SearchHit],
        source_ids: tuple[str, ...] = (),
        source_kinds: tuple[SourceKind, ...] = (),
        freshness: tuple[FreshnessState, ...] = (),
        why_matched: str,
        result_set_id: str | None = None,
    ) -> ResearchResultSet:
        stable_result_set_id = result_set_id.strip() if result_set_id is not None else None
        if result_set_id is not None and not stable_result_set_id:
            raise ValueError("result_set_id must not be empty")
        if stable_result_set_id is not None:
            try:
                existing = self._network.get_result_set(stable_result_set_id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing.workspace_id != workspace_id or existing.query != query:
                    raise ValueError("result_set_id already exists for a different query")
                return existing

        source_id_set = set(source_ids)
        source_kind_set = set(source_kinds)
        freshness_set = set(freshness)
        scoped = bool(source_id_set or source_kind_set or freshness_set)

        prepared: list[tuple[SearchHit, tuple[ResearchEvidence, ...]]] = []
        for hit in hits:
            evidence = tuple(
                item
                for item in self._network.evidence_for_document(hit.document_id)
                if self._allowed(
                    item,
                    source_ids=source_id_set,
                    source_kinds=source_kind_set,
                    freshness=freshness_set,
                )
            )
            if scoped and not evidence:
                raise RuntimeError(
                    "query result matched a source filter but retained no matching provenance"
                )
            prepared.append((hit, evidence))

        saved_result_set_id = stable_result_set_id or uuid4().hex
        created_at = _now()
        items: list[ResearchResultItem] = []
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_result_sets(
                    result_set_id, workspace_id, query, created_at
                ) VALUES (?, ?, ?, ?)""",
                (saved_result_set_id, workspace_id, query, created_at),
            )
            for ordinal, (hit, evidence) in enumerate(prepared):
                conn.execute(
                    """INSERT INTO research_result_items(
                        result_set_id, ordinal, document_id, title, snippet, rank,
                        why_matched, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        saved_result_set_id,
                        ordinal,
                        hit.document_id,
                        hit.title,
                        hit.snippet,
                        hit.rank,
                        why_matched,
                        _evidence_json(evidence),
                    ),
                )
                items.append(
                    ResearchResultItem(
                        ordinal=ordinal,
                        document_id=hit.document_id,
                        title=hit.title,
                        snippet=hit.snippet,
                        rank=hit.rank,
                        why_matched=why_matched,
                        evidence=evidence,
                    )
                )
        return ResearchResultSet(
            result_set_id=saved_result_set_id,
            workspace_id=workspace_id,
            query=query,
            items=tuple(items),
            created_at=created_at,
        )

    @staticmethod
    def _allowed(
        evidence: ResearchEvidence,
        *,
        source_ids: set[str],
        source_kinds: set[SourceKind],
        freshness: set[FreshnessState],
    ) -> bool:
        if source_ids and evidence.source_id not in source_ids:
            return False
        if source_kinds and evidence.source_kind not in source_kinds:
            return False
        if freshness:
            if evidence.source_kind is not SourceKind.HTTP:
                return False
            if evidence.freshness not in freshness:
                return False
        return True
