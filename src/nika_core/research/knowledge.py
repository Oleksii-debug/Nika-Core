from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from nika_core.research.chunking import ChunkPolicy, chunk_text_spans
from nika_core.research.normalize import normalize_text

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
NORMALIZATION_VERSION = "nika-normalize-nfkc-v1"
CHUNKER_VERSION = "nika-research-chunker-v1"


class ConnectionProvider(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


class KnowledgeVisibility(StrEnum):
    WORKSPACE = "workspace"
    RESTRICTED = "restricted"


class KnowledgeIngestDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"
    VERSIONED = "versioned"


class CorpusCorruptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    principal_id: str
    workspace_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id is required")
        if not self.workspace_ids:
            raise ValueError("at least one workspace_id is required")
        cleaned = tuple(workspace.strip() for workspace in self.workspace_ids)
        if any(not workspace for workspace in cleaned):
            raise ValueError("workspace_ids must not contain empty values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("workspace_ids must be unique")
        object.__setattr__(self, "workspace_ids", tuple(sorted(cleaned)))


@dataclass(frozen=True, slots=True)
class KnowledgeIngestRequest:
    workspace_id: str
    artifact_key: str
    title: str
    media_type: str
    text: str
    source_locator: str
    parser_name: str
    parser_version: str
    approved_by: str
    source_id: str | None = None
    raw_sha256: str | None = None
    visibility: KnowledgeVisibility = KnowledgeVisibility.WORKSPACE
    allowed_principals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "workspace_id": self.workspace_id,
            "artifact_key": self.artifact_key,
            "title": self.title,
            "media_type": self.media_type,
            "source_locator": self.source_locator,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "approved_by": self.approved_by,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.visibility, KnowledgeVisibility):
            raise TypeError("visibility must be a KnowledgeVisibility value")
        if self.source_id is not None and not self.source_id.strip():
            raise ValueError("source_id must be non-empty when provided")
        if self.raw_sha256 is not None and not _SHA256.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a lowercase SHA-256 hex digest")
        principals = tuple(principal.strip() for principal in self.allowed_principals)
        if any(not principal for principal in principals):
            raise ValueError("allowed_principals must not contain empty values")
        if len(set(principals)) != len(principals):
            raise ValueError("allowed_principals must be unique")
        principals = tuple(sorted(principals))
        if self.visibility is KnowledgeVisibility.WORKSPACE and principals:
            raise ValueError("workspace-visible artifacts must not define restricted ACL grants")
        object.__setattr__(self, "allowed_principals", principals)


@dataclass(frozen=True, slots=True)
class KnowledgeVersionRef:
    workspace_id: str
    artifact_key: str
    version: int
    normalized_sha256: str
    raw_sha256: str | None
    visibility: KnowledgeVisibility
    disposition: KnowledgeIngestDisposition


@dataclass(frozen=True, slots=True)
class KnowledgeProvenance:
    workspace_id: str
    artifact_key: str
    version: int
    source_id: str | None
    source_locator: str
    normalized_sha256: str
    raw_sha256: str | None
    parser_name: str
    parser_version: str
    normalization_version: str
    chunker_version: str
    chunk_max_chars: int
    chunk_overlap_chars: int
    approved_by: str
    chunk_id: str
    chunk_ordinal: int
    start_char: int
    end_char: int
    chunk_sha256: str


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    title: str
    text: str
    snippet: str
    rank: float
    provenance: KnowledgeProvenance


@dataclass(frozen=True, slots=True)
class CorpusIntegrityReport:
    versions_checked: int
    chunks_checked: int
    fts_rows_checked: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _safe_fts_query(query: str) -> str:
    terms = [term for term in normalize_text(query).replace("\n", " ").split(" ") if term]
    if not terms:
        raise ValueError("search query must not be empty")
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _chunk_id(
    workspace_id: str,
    artifact_key: str,
    version: int,
    ordinal: int,
    chunk_sha256: str,
) -> str:
    framed = (
        f"nika-knowledge-chunk-v1\0{workspace_id}\0{artifact_key}\0"
        f"{version}\0{ordinal}\0{chunk_sha256}"
    )
    return hashlib.sha256(framed.encode()).hexdigest()


class KnowledgeCorpus:
    def __init__(self, store: ConnectionProvider) -> None:
        self._store = store

    def ingest(
        self,
        request: KnowledgeIngestRequest,
        *,
        chunk_policy: ChunkPolicy | None = None,
    ) -> KnowledgeVersionRef:
        normalized = normalize_text(request.text)
        active_chunk_policy = chunk_policy or ChunkPolicy()
        if not normalized:
            raise ValueError("document contains no indexable text")
        normalized_sha256 = _sha256(normalized)
        now = _now()

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workspace = conn.execute(
                "SELECT 1 FROM research_workspaces WHERE workspace_id=?",
                (request.workspace_id,),
            ).fetchone()
            if workspace is None:
                raise ValueError("unknown research workspace")
            self._validate_source_provenance(conn, request)

            artifact = conn.execute(
                """SELECT current_version, visibility FROM knowledge_artifacts
                WHERE workspace_id=? AND artifact_key=?""",
                (request.workspace_id, request.artifact_key),
            ).fetchone()
            if artifact is None:
                version = 1
                disposition = KnowledgeIngestDisposition.CREATED
                conn.execute(
                    """INSERT INTO knowledge_artifacts(
                        workspace_id, artifact_key, current_version, visibility,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        request.workspace_id,
                        request.artifact_key,
                        version,
                        request.visibility.value,
                        now,
                        now,
                    ),
                )
            else:
                current_version = int(artifact["current_version"])
                current = conn.execute(
                    """SELECT normalized_sha256, raw_sha256, title, media_type, source_id,
                    source_locator, parser_name, parser_version, approved_by, normalization_version,
                    chunker_version, chunk_max_chars, chunk_overlap_chars, normalized_text
                    FROM knowledge_versions
                    WHERE workspace_id=? AND artifact_key=? AND version=?""",
                    (request.workspace_id, request.artifact_key, current_version),
                ).fetchone()
                if current is None:
                    raise CorpusCorruptionError("current knowledge version is missing")
                if _sha256(current["normalized_text"]) != current["normalized_sha256"]:
                    raise CorpusCorruptionError("current knowledge version hash mismatch")
                same_version_identity = (
                    current["normalized_sha256"] == normalized_sha256
                    and current["raw_sha256"] == request.raw_sha256
                    and current["title"] == request.title
                    and current["media_type"] == request.media_type
                    and current["source_id"] == request.source_id
                    and current["source_locator"] == request.source_locator
                    and current["parser_name"] == request.parser_name
                    and current["parser_version"] == request.parser_version
                    and current["approved_by"] == request.approved_by
                    and current["normalization_version"] == NORMALIZATION_VERSION
                    and current["chunker_version"] == CHUNKER_VERSION
                    and int(current["chunk_max_chars"]) == active_chunk_policy.max_chars
                    and int(current["chunk_overlap_chars"]) == active_chunk_policy.overlap_chars
                )
                if same_version_identity:
                    self._assert_duplicate_acl(conn, request, artifact["visibility"])
                    conn.execute(
                        """UPDATE knowledge_artifacts SET updated_at=?
                        WHERE workspace_id=? AND artifact_key=?""",
                        (now, request.workspace_id, request.artifact_key),
                    )
                    return KnowledgeVersionRef(
                        workspace_id=request.workspace_id,
                        artifact_key=request.artifact_key,
                        version=current_version,
                        normalized_sha256=normalized_sha256,
                        raw_sha256=current["raw_sha256"],
                        visibility=KnowledgeVisibility(artifact["visibility"]),
                        disposition=KnowledgeIngestDisposition.DEDUPLICATED,
                    )
                version = current_version + 1
                disposition = KnowledgeIngestDisposition.VERSIONED

            conn.execute(
                """INSERT INTO knowledge_versions(
                    workspace_id, artifact_key, version, normalized_sha256, raw_sha256,
                    title, media_type, source_id, source_locator, parser_name, parser_version,
                    normalization_version, chunker_version, chunk_max_chars, chunk_overlap_chars,
                    approved_by, normalized_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.workspace_id,
                    request.artifact_key,
                    version,
                    normalized_sha256,
                    request.raw_sha256,
                    request.title,
                    request.media_type,
                    request.source_id,
                    request.source_locator,
                    request.parser_name,
                    request.parser_version,
                    NORMALIZATION_VERSION,
                    CHUNKER_VERSION,
                    active_chunk_policy.max_chars,
                    active_chunk_policy.overlap_chars,
                    request.approved_by,
                    normalized,
                    now,
                ),
            )
            chunks = chunk_text_spans(normalized, policy=active_chunk_policy)
            for ordinal, chunk in enumerate(chunks):
                chunk_sha256 = _sha256(chunk.text)
                chunk_id = _chunk_id(
                    request.workspace_id,
                    request.artifact_key,
                    version,
                    ordinal,
                    chunk_sha256,
                )
                conn.execute(
                    """INSERT INTO knowledge_chunks(
                        chunk_id, workspace_id, artifact_key, version, ordinal,
                        start_char, end_char, chunk_sha256, text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        request.workspace_id,
                        request.artifact_key,
                        version,
                        ordinal,
                        chunk.start_char,
                        chunk.end_char,
                        chunk_sha256,
                        chunk.text,
                    ),
                )
                conn.execute(
                    """INSERT INTO knowledge_fts(
                        workspace_id, artifact_key, version, ordinal, chunk_id, title, body
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        request.workspace_id,
                        request.artifact_key,
                        str(version),
                        str(ordinal),
                        chunk_id,
                        request.title,
                        chunk.text,
                    ),
                )

            self._replace_acl(conn, request, now)
            conn.execute(
                """UPDATE knowledge_artifacts
                SET current_version=?, visibility=?, updated_at=?
                WHERE workspace_id=? AND artifact_key=?""",
                (
                    version,
                    request.visibility.value,
                    now,
                    request.workspace_id,
                    request.artifact_key,
                ),
            )

        return KnowledgeVersionRef(
            workspace_id=request.workspace_id,
            artifact_key=request.artifact_key,
            version=version,
            normalized_sha256=normalized_sha256,
            raw_sha256=request.raw_sha256,
            visibility=request.visibility,
            disposition=disposition,
        )

    @staticmethod
    def _validate_source_provenance(
        conn: sqlite3.Connection,
        request: KnowledgeIngestRequest,
    ) -> None:
        if request.source_id is None:
            return
        local_rows = conn.execute(
            """SELECT workspace_id, locator FROM research_sources
            WHERE source_id=?""",
            (request.source_id,),
        ).fetchall()
        http_rows = conn.execute(
            """SELECT workspace_id, url AS locator FROM research_http_sources
            WHERE source_id=?""",
            (request.source_id,),
        ).fetchall()
        source_rows = tuple(local_rows) + tuple(http_rows)
        if len(source_rows) != 1:
            raise ValueError("knowledge source identity is missing or ambiguous")
        source = source_rows[0]
        if source["workspace_id"] != request.workspace_id:
            raise PermissionError("knowledge source identity crosses workspace boundary")
        if source["locator"] != request.source_locator:
            raise ValueError("knowledge source locator does not match durable source identity")

    @staticmethod
    def _assert_duplicate_acl(
        conn: sqlite3.Connection,
        request: KnowledgeIngestRequest,
        visibility: str,
    ) -> None:
        existing = conn.execute(
            """SELECT principal_id FROM knowledge_acl
            WHERE workspace_id=? AND artifact_key=? ORDER BY principal_id""",
            (request.workspace_id, request.artifact_key),
        ).fetchall()
        existing_principals = tuple(row["principal_id"] for row in existing)
        if (
            visibility != request.visibility.value
            or existing_principals != request.allowed_principals
        ):
            raise ValueError("duplicate ingestion may not silently mutate knowledge permissions")

    @staticmethod
    def _replace_acl(
        conn: sqlite3.Connection,
        request: KnowledgeIngestRequest,
        now: str,
    ) -> None:
        conn.execute(
            "DELETE FROM knowledge_acl WHERE workspace_id=? AND artifact_key=?",
            (request.workspace_id, request.artifact_key),
        )
        if request.visibility is not KnowledgeVisibility.RESTRICTED:
            return
        conn.executemany(
            """INSERT INTO knowledge_acl(
                workspace_id, artifact_key, principal_id, created_at
            ) VALUES (?, ?, ?, ?)""",
            (
                (request.workspace_id, request.artifact_key, principal_id, now)
                for principal_id in request.allowed_principals
            ),
        )

    def search(
        self,
        scope: RetrievalScope,
        query: str,
        *,
        limit: int = 20,
    ) -> list[KnowledgeHit]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValueError("limit must be an integer between 1 and 100")
        fts_query = _safe_fts_query(query)
        placeholders = ",".join("?" for _ in scope.workspace_ids)
        sql = f"""SELECT
            knowledge_fts.workspace_id,
            knowledge_fts.artifact_key,
            CAST(knowledge_fts.version AS INTEGER) AS version,
            CAST(knowledge_fts.ordinal AS INTEGER) AS ordinal,
            knowledge_fts.chunk_id,
            knowledge_fts.title AS indexed_title,
            knowledge_fts.body AS indexed_body,
            snippet(knowledge_fts, 6, '[', ']', ' … ', 24) AS snippet,
            bm25(knowledge_fts) AS rank,
            c.start_char, c.end_char, c.chunk_sha256, c.text,
            v.title, v.source_id, v.source_locator, v.normalized_sha256, v.raw_sha256,
            v.parser_name, v.parser_version, v.normalization_version, v.chunker_version,
            v.chunk_max_chars, v.chunk_overlap_chars, v.approved_by, v.normalized_text
        FROM knowledge_fts
        JOIN knowledge_artifacts AS a
          ON a.workspace_id=knowledge_fts.workspace_id
         AND a.artifact_key=knowledge_fts.artifact_key
         AND a.current_version=CAST(knowledge_fts.version AS INTEGER)
        JOIN knowledge_chunks AS c
          ON c.chunk_id=knowledge_fts.chunk_id
         AND c.workspace_id=knowledge_fts.workspace_id
         AND c.artifact_key=knowledge_fts.artifact_key
         AND c.version=CAST(knowledge_fts.version AS INTEGER)
         AND c.ordinal=CAST(knowledge_fts.ordinal AS INTEGER)
        JOIN knowledge_versions AS v
          ON v.workspace_id=knowledge_fts.workspace_id
         AND v.artifact_key=knowledge_fts.artifact_key
         AND v.version=CAST(knowledge_fts.version AS INTEGER)
        WHERE knowledge_fts MATCH ?
          AND knowledge_fts.workspace_id IN ({placeholders})
          AND (
            a.visibility='workspace'
            OR EXISTS (
                SELECT 1 FROM knowledge_acl AS acl
                WHERE acl.workspace_id=a.workspace_id
                  AND acl.artifact_key=a.artifact_key
                  AND acl.principal_id=?
            )
          )
        ORDER BY rank, knowledge_fts.artifact_key, version, ordinal, knowledge_fts.chunk_id
        LIMIT ?"""
        params: list[object] = [fts_query, *scope.workspace_ids, scope.principal_id, limit]
        with self._store.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        checked_versions: set[tuple[str, str, int]] = set()
        hits: list[KnowledgeHit] = []
        for row in rows:
            version_key = (row["workspace_id"], row["artifact_key"], int(row["version"]))
            if version_key not in checked_versions:
                if _sha256(row["normalized_text"]) != row["normalized_sha256"]:
                    raise CorpusCorruptionError("knowledge version hash mismatch")
                checked_versions.add(version_key)
            self._verify_hit_row(row)
            hits.append(
                KnowledgeHit(
                    title=row["title"],
                    text=row["text"],
                    snippet=row["snippet"],
                    rank=float(row["rank"]),
                    provenance=KnowledgeProvenance(
                        workspace_id=row["workspace_id"],
                        artifact_key=row["artifact_key"],
                        version=int(row["version"]),
                        source_id=row["source_id"],
                        source_locator=row["source_locator"],
                        normalized_sha256=row["normalized_sha256"],
                        raw_sha256=row["raw_sha256"],
                        parser_name=row["parser_name"],
                        parser_version=row["parser_version"],
                        normalization_version=row["normalization_version"],
                        chunker_version=row["chunker_version"],
                        chunk_max_chars=int(row["chunk_max_chars"]),
                        chunk_overlap_chars=int(row["chunk_overlap_chars"]),
                        approved_by=row["approved_by"],
                        chunk_id=row["chunk_id"],
                        chunk_ordinal=int(row["ordinal"]),
                        start_char=int(row["start_char"]),
                        end_char=int(row["end_char"]),
                        chunk_sha256=row["chunk_sha256"],
                    ),
                )
            )
        return hits

    @staticmethod
    def _verify_hit_row(row: sqlite3.Row) -> None:
        text = row["text"]
        if row["indexed_title"] != row["title"]:
            raise CorpusCorruptionError("FTS title does not match authoritative version title")
        if row["indexed_body"] != text:
            raise CorpusCorruptionError("FTS body does not match authoritative chunk text")
        if _sha256(text) != row["chunk_sha256"]:
            raise CorpusCorruptionError("knowledge chunk hash mismatch")
        start = int(row["start_char"])
        end = int(row["end_char"])
        normalized = row["normalized_text"]
        if start < 0 or end < start or end > len(normalized):
            raise CorpusCorruptionError("knowledge chunk boundaries are invalid")
        if normalized[start:end] != text:
            raise CorpusCorruptionError("knowledge chunk boundaries do not match source text")

    def version_numbers(self, workspace_id: str, artifact_key: str) -> tuple[int, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT version FROM knowledge_versions
                WHERE workspace_id=? AND artifact_key=? ORDER BY version""",
                (workspace_id, artifact_key),
            ).fetchall()
        return tuple(int(row["version"]) for row in rows)

    def verify_integrity(
        self,
        *,
        workspace_ids: Iterable[str] | None = None,
    ) -> CorpusIntegrityReport:
        selected = tuple(sorted(set(workspace_ids or ())))
        if any(not workspace.strip() for workspace in selected):
            raise ValueError("workspace_ids must not contain empty values")
        with self._store.connection() as conn:
            quick = conn.execute("PRAGMA quick_check").fetchall()
            if not quick or any(row[0] != "ok" for row in quick):
                raise CorpusCorruptionError("SQLite quick_check failed")
            where = ""
            params: tuple[object, ...] = ()
            if selected:
                placeholders = ",".join("?" for _ in selected)
                where = f" WHERE workspace_id IN ({placeholders})"
                params = selected
            versions = conn.execute(
                "SELECT * FROM knowledge_versions"
                + where
                + " ORDER BY workspace_id, artifact_key, version",
                params,
            ).fetchall()
            chunks = conn.execute(
                "SELECT * FROM knowledge_chunks"
                + where
                + " ORDER BY workspace_id, artifact_key, version, ordinal",
                params,
            ).fetchall()
            artifacts = conn.execute(
                "SELECT * FROM knowledge_artifacts"
                + where
                + " ORDER BY workspace_id, artifact_key",
                params,
            ).fetchall()

            version_map = {
                (row["workspace_id"], row["artifact_key"], int(row["version"])): row
                for row in versions
            }
            for artifact in artifacts:
                key = (
                    artifact["workspace_id"],
                    artifact["artifact_key"],
                    int(artifact["current_version"]),
                )
                if key not in version_map:
                    raise CorpusCorruptionError(
                        "knowledge artifact points to a missing current version"
                    )
            for version in versions:
                if _sha256(version["normalized_text"]) != version["normalized_sha256"]:
                    raise CorpusCorruptionError("knowledge version hash mismatch")

            chunk_map = {row["chunk_id"]: row for row in chunks}
            for chunk in chunks:
                key = (chunk["workspace_id"], chunk["artifact_key"], int(chunk["version"]))
                version = version_map.get(key)
                if version is None:
                    raise CorpusCorruptionError("knowledge chunk points to a missing version")
                if _sha256(chunk["text"]) != chunk["chunk_sha256"]:
                    raise CorpusCorruptionError("knowledge chunk hash mismatch")
                start = int(chunk["start_char"])
                end = int(chunk["end_char"])
                if start < 0 or end < start or end > len(version["normalized_text"]):
                    raise CorpusCorruptionError("knowledge chunk boundaries are invalid")
                if version["normalized_text"][start:end] != chunk["text"]:
                    raise CorpusCorruptionError(
                        "knowledge chunk boundaries do not match source text"
                    )

            fts_where = ""
            fts_params: tuple[object, ...] = ()
            if selected:
                placeholders = ",".join("?" for _ in selected)
                fts_where = f" WHERE workspace_id IN ({placeholders})"
                fts_params = selected
            fts_rows = conn.execute(
                "SELECT workspace_id, artifact_key, version, ordinal, chunk_id, title, body "
                "FROM knowledge_fts" + fts_where + " ORDER BY rowid",
                fts_params,
            ).fetchall()

            seen_fts: set[str] = set()
            for fts in fts_rows:
                chunk = chunk_map.get(fts["chunk_id"])
                if chunk is None:
                    raise CorpusCorruptionError("knowledge FTS row has no authoritative chunk")
                if fts["chunk_id"] in seen_fts:
                    raise CorpusCorruptionError("knowledge FTS row is duplicated")
                seen_fts.add(fts["chunk_id"])
                version_key = (
                    chunk["workspace_id"],
                    chunk["artifact_key"],
                    int(chunk["version"]),
                )
                version = version_map.get(version_key)
                if version is None:
                    raise CorpusCorruptionError("knowledge FTS row points to a missing version")
                if (
                    fts["workspace_id"] != chunk["workspace_id"]
                    or fts["artifact_key"] != chunk["artifact_key"]
                    or int(fts["version"]) != int(chunk["version"])
                    or int(fts["ordinal"]) != int(chunk["ordinal"])
                    or fts["title"] != version["title"]
                    or fts["body"] != chunk["text"]
                ):
                    raise CorpusCorruptionError("knowledge FTS metadata is stale or mismatched")

            missing_fts = set(chunk_map).difference(seen_fts)
            if missing_fts:
                raise CorpusCorruptionError("knowledge FTS row is missing")

        return CorpusIntegrityReport(
            versions_checked=len(versions),
            chunks_checked=len(chunks),
            fts_rows_checked=len(fts_rows),
        )
