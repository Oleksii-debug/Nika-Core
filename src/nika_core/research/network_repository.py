from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    FreshnessState,
    HttpSourceState,
    RefreshDisposition,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SearchHit,
    SourceKind,
    SourceSpec,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _snapshot_id(source_id: str, raw_sha256: str) -> str:
    return hashlib.sha256(f"{source_id}\0{raw_sha256}".encode()).hexdigest()


class NetworkResearchRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def register_source(self, source: SourceSpec) -> HttpSourceState:
        if source.kind is not SourceKind.HTTP:
            raise ValueError("network repository only accepts HTTP sources")
        if not source.source_id.strip() or not source.workspace_id.strip() or not source.locator.strip():
            raise ValueError("source_id, workspace_id and URL are required")
        now = _now()
        with self._store.connection() as conn:
            collision = conn.execute(
                "SELECT 1 FROM research_sources WHERE source_id=?",
                (source.source_id,),
            ).fetchone()
            if collision is not None:
                raise ValueError("source_id is already owned by a local source")
            existing = conn.execute(
                "SELECT url FROM research_http_sources WHERE source_id=?",
                (source.source_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO research_http_sources(
                        source_id, workspace_id, url, freshness, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source.source_id,
                        source.workspace_id,
                        source.locator,
                        FreshnessState.UNKNOWN.value,
                        now,
                        now,
                    ),
                )
            elif existing["url"] == source.locator:
                conn.execute(
                    """UPDATE research_http_sources
                    SET workspace_id=?, updated_at=? WHERE source_id=?""",
                    (source.workspace_id, now, source.source_id),
                )
            else:
                conn.execute(
                    """UPDATE research_http_sources SET
                        workspace_id=?, url=?, final_url=NULL, etag=NULL, last_modified=NULL,
                        current_raw_sha256=NULL, freshness=?, last_attempt_at=NULL,
                        last_success_at=NULL, last_status_code=NULL, last_error_code=NULL,
                        last_error_message=NULL, updated_at=?
                    WHERE source_id=?""",
                    (
                        source.workspace_id,
                        source.locator,
                        FreshnessState.UNKNOWN.value,
                        now,
                        source.source_id,
                    ),
                )
        return self.get_source(source.source_id)

    def get_source(self, source_id: str) -> HttpSourceState:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM research_http_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown HTTP source: {source_id}")
        return HttpSourceState(
            source_id=row["source_id"],
            workspace_id=row["workspace_id"],
            url=row["url"],
            final_url=row["final_url"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            current_raw_sha256=row["current_raw_sha256"],
            freshness=FreshnessState(row["freshness"]),
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            last_status_code=row["last_status_code"],
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
        )

    def list_sources(
        self,
        workspace_id: str,
        *,
        source_ids: tuple[str, ...] | None = None,
    ) -> tuple[HttpSourceState, ...]:
        with self._store.connection() as conn:
            if source_ids is None:
                rows = conn.execute(
                    """SELECT source_id FROM research_http_sources
                    WHERE workspace_id=? ORDER BY source_id""",
                    (workspace_id,),
                ).fetchall()
                ids = tuple(row["source_id"] for row in rows)
            else:
                ids = source_ids
        sources = tuple(self.get_source(source_id) for source_id in ids)
        if any(source.workspace_id != workspace_id for source in sources):
            raise ValueError("refresh source set crosses workspace boundary")
        return sources

    def record_attempt(
        self,
        *,
        source_id: str,
        attempt_number: int,
        disposition: RefreshDisposition,
        requested_url: str,
        final_url: str,
        status_code: int | None,
        error_code: str | None,
        error_message: str,
        retryable: bool,
        task_id: str | None = None,
    ) -> str:
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        attempt_id = uuid4().hex
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_http_attempts(
                    attempt_id, task_id, source_id, attempt_number, disposition,
                    status_code, requested_url, final_url, error_code, error_message,
                    retryable, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    task_id,
                    source_id,
                    attempt_number,
                    disposition.value,
                    status_code,
                    requested_url,
                    final_url,
                    error_code,
                    error_message,
                    int(retryable),
                    _now(),
                ),
            )
        return attempt_id

    def finalize_source(
        self,
        source_id: str,
        *,
        disposition: RefreshDisposition,
        final_url: str | None,
        status_code: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
        current_raw_sha256: str | None = None,
        error_code: str | None = None,
        error_message: str = "",
    ) -> HttpSourceState:
        current = self.get_source(source_id)
        now = _now()
        success = disposition in {
            RefreshDisposition.CHANGED,
            RefreshDisposition.UNCHANGED,
            RefreshDisposition.NOT_MODIFIED,
            RefreshDisposition.DYNAMIC_REQUIRED,
        }
        if success:
            freshness = FreshnessState.CURRENT
        elif disposition is RefreshDisposition.REMOVED:
            freshness = FreshnessState.REMOVED
        elif disposition is RefreshDisposition.BLOCKED:
            freshness = FreshnessState.BLOCKED
        elif current.current_raw_sha256 is not None:
            freshness = FreshnessState.STALE
        else:
            freshness = FreshnessState.ERROR
        with self._store.connection() as conn:
            conn.execute(
                """UPDATE research_http_sources SET
                    final_url=?, etag=?, last_modified=?, current_raw_sha256=?,
                    freshness=?, last_attempt_at=?, last_success_at=?, last_status_code=?,
                    last_error_code=?, last_error_message=?, updated_at=?
                WHERE source_id=?""",
                (
                    final_url or current.final_url,
                    etag if etag is not None else current.etag,
                    last_modified if last_modified is not None else current.last_modified,
                    (
                        current_raw_sha256
                        if current_raw_sha256 is not None
                        else current.current_raw_sha256
                    ),
                    freshness.value,
                    now,
                    now if success else current.last_success_at,
                    status_code,
                    error_code,
                    error_message,
                    now,
                    source_id,
                ),
            )
        return self.get_source(source_id)

    def record_snapshot(
        self,
        *,
        source_id: str,
        artifact_id: str,
        raw_sha256: str,
        media_type: str,
        etag: str | None,
        last_modified: str | None,
        extraction_id: str | None,
        document_id: str | None,
    ) -> str:
        snapshot_id = _snapshot_id(source_id, raw_sha256)
        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_http_snapshots(
                    snapshot_id, source_id, artifact_id, raw_sha256, media_type,
                    etag, last_modified, extraction_id, document_id, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    extraction_id=COALESCE(research_http_snapshots.extraction_id, excluded.extraction_id),
                    document_id=COALESCE(research_http_snapshots.document_id, excluded.document_id)""",
                (
                    snapshot_id,
                    source_id,
                    artifact_id,
                    raw_sha256,
                    media_type,
                    etag,
                    last_modified,
                    extraction_id,
                    document_id,
                    now,
                ),
            )
            row = conn.execute(
                """SELECT artifact_id, raw_sha256 FROM research_http_snapshots
                WHERE snapshot_id=?""",
                (snapshot_id,),
            ).fetchone()
        if row is None or row["artifact_id"] != artifact_id or row["raw_sha256"] != raw_sha256:
            raise RuntimeError("HTTP snapshot identity collision")
        return snapshot_id

    def link_document_origin(
        self,
        *,
        document_id: str,
        source_id: str,
        snapshot_id: str,
        locator: str,
    ) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO corpus_http_origins(
                    document_id, source_id, snapshot_id, locator, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(document_id, source_id, snapshot_id) DO NOTHING""",
                (document_id, source_id, snapshot_id, locator, _now()),
            )

    def evidence_for_document(self, document_id: str) -> tuple[ResearchEvidence, ...]:
        with self._store.connection() as conn:
            local_rows = conn.execute(
                """SELECT o.source_id, o.locator, o.observed_at
                FROM corpus_origins o
                WHERE o.document_id=? ORDER BY o.observed_at, o.source_id, o.locator""",
                (document_id,),
            ).fetchall()
            http_rows = conn.execute(
                """SELECT o.source_id, o.locator, o.observed_at
                FROM corpus_http_origins o
                WHERE o.document_id=? ORDER BY o.observed_at, o.source_id, o.locator""",
                (document_id,),
            ).fetchall()
        evidence = [
            ResearchEvidence(
                source_id=row["source_id"],
                source_kind=SourceKind.LOCAL_FILE,
                locator=row["locator"],
                observed_at=row["observed_at"],
            )
            for row in local_rows
        ]
        evidence.extend(
            ResearchEvidence(
                source_id=row["source_id"],
                source_kind=SourceKind.HTTP,
                locator=row["locator"],
                observed_at=row["observed_at"],
            )
            for row in http_rows
        )
        return tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.observed_at,
                    item.source_kind.value,
                    item.source_id,
                    item.locator,
                ),
            )
        )

    def save_result_set(
        self,
        *,
        workspace_id: str,
        query: str,
        hits: list[SearchHit],
    ) -> ResearchResultSet:
        result_set_id = uuid4().hex
        created_at = _now()
        items: list[ResearchResultItem] = []
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_result_sets(result_set_id, workspace_id, query, created_at)
                VALUES (?, ?, ?, ?)""",
                (result_set_id, workspace_id, query, created_at),
            )
            for ordinal, hit in enumerate(hits):
                evidence = self.evidence_for_document(hit.document_id)
                why_matched = f"Literal-token full-text match for: {query}"
                evidence_json = json.dumps(
                    [
                        {
                            "source_id": item.source_id,
                            "source_kind": item.source_kind.value,
                            "locator": item.locator,
                            "observed_at": item.observed_at,
                        }
                        for item in evidence
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                conn.execute(
                    """INSERT INTO research_result_items(
                        result_set_id, ordinal, document_id, title, snippet, rank,
                        why_matched, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result_set_id,
                        ordinal,
                        hit.document_id,
                        hit.title,
                        hit.snippet,
                        hit.rank,
                        why_matched,
                        evidence_json,
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
            result_set_id=result_set_id,
            workspace_id=workspace_id,
            query=query,
            items=tuple(items),
            created_at=created_at,
        )

    def get_result_set(self, result_set_id: str) -> ResearchResultSet:
        with self._store.connection() as conn:
            header = conn.execute(
                "SELECT * FROM research_result_sets WHERE result_set_id=?",
                (result_set_id,),
            ).fetchone()
            rows = conn.execute(
                """SELECT * FROM research_result_items
                WHERE result_set_id=? ORDER BY ordinal""",
                (result_set_id,),
            ).fetchall()
        if header is None:
            raise KeyError(f"unknown research result set: {result_set_id}")
        items: list[ResearchResultItem] = []
        for row in rows:
            raw_evidence = json.loads(row["evidence_json"])
            evidence = tuple(
                ResearchEvidence(
                    source_id=item["source_id"],
                    source_kind=SourceKind(item["source_kind"]),
                    locator=item["locator"],
                    observed_at=item["observed_at"],
                )
                for item in raw_evidence
            )
            items.append(
                ResearchResultItem(
                    ordinal=row["ordinal"],
                    document_id=row["document_id"],
                    title=row["title"],
                    snippet=row["snippet"],
                    rank=float(row["rank"]),
                    why_matched=row["why_matched"],
                    evidence=evidence,
                )
            )
        return ResearchResultSet(
            result_set_id=header["result_set_id"],
            workspace_id=header["workspace_id"],
            query=header["query"],
            items=tuple(items),
            created_at=header["created_at"],
        )

    def attempt_count(self, source_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM research_http_attempts WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return int(row["count"])

    def snapshot_count(self, source_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM research_http_snapshots WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return int(row["count"])
