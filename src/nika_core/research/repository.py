from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.chunking import chunk_text
from nika_core.research.models import (
    BlobArtifact,
    CorpusDocument,
    ExtractedDocument,
    ExtractionRecord,
    ExtractionStatus,
    IngestDisposition,
    IngestResult,
    ResearchWorkspace,
    SearchHit,
    SourceSpec,
)
from nika_core.research.normalize import normalize_text


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_fts_query(query: str) -> str:
    terms = [term for term in normalize_text(query).replace("\n", " ").split(" ") if term]
    if not terms:
        raise ValueError("search query must not be empty")
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _extraction_id(
    artifact_id: str,
    extractor: str,
    extractor_version: str,
) -> str:
    identity = f"{artifact_id}\0{extractor}\0{extractor_version}"
    return hashlib.sha256(identity.encode()).hexdigest()


class ResearchRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def upsert_workspace(self, workspace: ResearchWorkspace) -> None:
        if not workspace.workspace_id.strip() or not workspace.name.strip():
            raise ValueError("workspace_id and name are required")
        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    name=excluded.name, updated_at=excluded.updated_at""",
                (workspace.workspace_id, workspace.name, now, now),
            )

    def upsert_source(self, source: SourceSpec) -> None:
        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_sources(
                    source_id, workspace_id, kind, locator, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    kind=excluded.kind,
                    locator=excluded.locator,
                    updated_at=excluded.updated_at""",
                (
                    source.source_id,
                    source.workspace_id,
                    source.kind.value,
                    source.locator,
                    now,
                    now,
                ),
            )

    def record_artifact(
        self,
        source: SourceSpec,
        artifact: BlobArtifact,
        *,
        media_type: str,
        original_name: str,
    ) -> BlobArtifact:
        if artifact.workspace_id != source.workspace_id:
            raise ValueError("artifact workspace does not match source workspace")
        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO corpus_artifacts(
                    artifact_id, workspace_id, raw_sha256, byte_size, media_type,
                    original_name, storage_relpath, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO NOTHING""",
                (
                    artifact.artifact_id,
                    artifact.workspace_id,
                    artifact.raw_sha256,
                    artifact.byte_size,
                    media_type,
                    original_name,
                    artifact.storage_relpath,
                    now,
                ),
            )
            row = conn.execute(
                """SELECT raw_sha256, byte_size, storage_relpath
                FROM corpus_artifacts WHERE artifact_id=?""",
                (artifact.artifact_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("artifact persistence failed")
            if (
                row["raw_sha256"] != artifact.raw_sha256
                or row["byte_size"] != artifact.byte_size
                or row["storage_relpath"] != artifact.storage_relpath
            ):
                raise RuntimeError("artifact identity collision")
            conn.execute(
                """INSERT INTO corpus_artifact_origins(
                    artifact_id, source_id, locator, observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(artifact_id, source_id, locator)
                DO UPDATE SET observed_at=excluded.observed_at""",
                (artifact.artifact_id, source.source_id, source.locator, now),
            )
        return artifact

    def record_extraction(
        self,
        *,
        artifact_id: str,
        extractor: str,
        extractor_version: str,
        status: ExtractionStatus,
        normalized_text_sha256: str | None,
        detail: str = "",
    ) -> ExtractionRecord:
        extraction_id = _extraction_id(artifact_id, extractor, extractor_version)
        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO corpus_extractions(
                    extraction_id, artifact_id, extractor, extractor_version, status,
                    normalized_text_sha256, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(extraction_id) DO UPDATE SET
                    status=excluded.status,
                    normalized_text_sha256=excluded.normalized_text_sha256,
                    detail=excluded.detail""",
                (
                    extraction_id,
                    artifact_id,
                    extractor,
                    extractor_version,
                    status.value,
                    normalized_text_sha256,
                    detail,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM corpus_extractions WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("extraction persistence failed")
        return ExtractionRecord(
            extraction_id=row["extraction_id"],
            artifact_id=row["artifact_id"],
            extractor=row["extractor"],
            extractor_version=row["extractor_version"],
            status=ExtractionStatus(row["status"]),
            normalized_text_sha256=row["normalized_text_sha256"],
            detail=row["detail"],
        )

    def link_document_artifact(
        self,
        *,
        document_id: str,
        artifact_id: str,
        extraction_id: str,
    ) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO corpus_document_artifacts(document_id, artifact_id, extraction_id)
                VALUES (?, ?, ?)
                ON CONFLICT(document_id, artifact_id, extraction_id) DO NOTHING""",
                (document_id, artifact_id, extraction_id),
            )

    def ingest_document(self, source: SourceSpec, extracted: ExtractedDocument) -> IngestResult:
        normalized = normalize_text(extracted.text)
        if not normalized:
            raise ValueError("document contains no indexable text")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        now = _now()
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM corpus_documents WHERE workspace_id=? AND normalized_sha256=?",
                (source.workspace_id, digest),
            ).fetchone()
            if row is None:
                document_id = uuid4().hex
                conn.execute(
                    """INSERT INTO corpus_documents(
                        document_id, workspace_id, normalized_sha256, title, media_type,
                        normalized_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_id,
                        source.workspace_id,
                        digest,
                        extracted.title,
                        extracted.media_type,
                        normalized,
                        now,
                    ),
                )
                for ordinal, chunk in enumerate(chunk_text(normalized)):
                    conn.execute(
                        """INSERT INTO corpus_chunks(chunk_id, document_id, ordinal, text)
                        VALUES (?, ?, ?, ?)""",
                        (uuid4().hex, document_id, ordinal, chunk),
                    )
                conn.execute(
                    """INSERT INTO corpus_fts(document_id, workspace_id, title, body)
                    VALUES (?, ?, ?, ?)""",
                    (document_id, source.workspace_id, extracted.title, normalized),
                )
                disposition = IngestDisposition.CREATED
                row = conn.execute(
                    "SELECT * FROM corpus_documents WHERE document_id=?", (document_id,)
                ).fetchone()
            else:
                disposition = IngestDisposition.DEDUPLICATED

            conn.execute(
                """INSERT INTO corpus_origins(document_id, source_id, locator, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id, source_id, locator)
                DO UPDATE SET observed_at=excluded.observed_at""",
                (row["document_id"], source.source_id, source.locator, now),
            )

        document = CorpusDocument(
            document_id=row["document_id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            normalized_sha256=row["normalized_sha256"],
            text=row["normalized_text"],
            media_type=row["media_type"],
        )
        return IngestResult(
            disposition=disposition,
            document=document,
            source_id=source.source_id,
            origin_locator=source.locator,
        )

    def search(self, workspace_id: str, query: str, *, limit: int = 20) -> list[SearchHit]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        fts_query = _safe_fts_query(query)
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT document_id, title,
                    snippet(corpus_fts, 3, '[', ']', ' … ', 24) AS snippet,
                    bm25(corpus_fts) AS rank
                FROM corpus_fts
                WHERE corpus_fts MATCH ? AND workspace_id=?
                ORDER BY rank, document_id
                LIMIT ?""",
                (fts_query, workspace_id, limit),
            ).fetchall()
        return [
            SearchHit(
                document_id=row["document_id"],
                title=row["title"],
                snippet=row["snippet"],
                rank=float(row["rank"]),
            )
            for row in rows
        ]

    def origin_count(self, document_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM corpus_origins WHERE document_id=?",
                (document_id,),
            ).fetchone()
        return int(row["count"])

    def artifact_origin_count(self, artifact_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS count
                FROM corpus_artifact_origins WHERE artifact_id=?""",
                (artifact_id,),
            ).fetchone()
        return int(row["count"])

    def chunk_texts(self, document_id: str) -> tuple[str, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT text FROM corpus_chunks
                WHERE document_id=? ORDER BY ordinal""",
                (document_id,),
            ).fetchall()
        return tuple(row["text"] for row in rows)
