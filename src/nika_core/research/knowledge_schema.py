from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

KNOWLEDGE_SCHEMA_VERSION = 3

KNOWLEDGE_MIGRATION_1 = (
    """CREATE TABLE IF NOT EXISTS knowledge_artifacts (
        workspace_id TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        current_version INTEGER NOT NULL CHECK(current_version >= 1),
        visibility TEXT NOT NULL CHECK(visibility IN ('workspace','restricted')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id, artifact_key),
        FOREIGN KEY(workspace_id) REFERENCES research_workspaces(workspace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_versions (
        workspace_id TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        normalized_sha256 TEXT NOT NULL,
        raw_sha256 TEXT,
        title TEXT NOT NULL,
        media_type TEXT NOT NULL,
        source_id TEXT,
        source_locator TEXT NOT NULL,
        parser_name TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        normalization_version TEXT NOT NULL,
        chunker_version TEXT NOT NULL,
        chunk_max_chars INTEGER NOT NULL CHECK(chunk_max_chars > 0),
        chunk_overlap_chars INTEGER NOT NULL CHECK(chunk_overlap_chars >= 0),
        approved_by TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id, artifact_key, version),
        FOREIGN KEY(workspace_id, artifact_key)
            REFERENCES knowledge_artifacts(workspace_id, artifact_key)
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_acl (
        workspace_id TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id, artifact_key, principal_id),
        FOREIGN KEY(workspace_id, artifact_key)
            REFERENCES knowledge_artifacts(workspace_id, artifact_key)
            ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_chunks (
        chunk_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        start_char INTEGER NOT NULL CHECK(start_char >= 0),
        end_char INTEGER NOT NULL CHECK(end_char >= start_char),
        chunk_sha256 TEXT NOT NULL,
        text TEXT NOT NULL,
        UNIQUE(workspace_id, artifact_key, version, ordinal),
        FOREIGN KEY(workspace_id, artifact_key, version)
            REFERENCES knowledge_versions(workspace_id, artifact_key, version)
            ON DELETE CASCADE
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
        workspace_id UNINDEXED,
        artifact_key UNINDEXED,
        version UNINDEXED,
        ordinal UNINDEXED,
        chunk_id UNINDEXED,
        title,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_current
    ON knowledge_artifacts(workspace_id, current_version)""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_versions
    ON knowledge_versions(workspace_id, artifact_key, version)""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_acl_principal
    ON knowledge_acl(principal_id, workspace_id, artifact_key)""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_version
    ON knowledge_chunks(workspace_id, artifact_key, version, ordinal)""",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _legacy_chunk_id(workspace_id: str, artifact_key: str, text: str) -> str:
    body_hash = hashlib.sha256(text.encode()).hexdigest()
    framed = f"legacy\0{workspace_id}\0{artifact_key}\0{1}\0{0}\0{body_hash}"
    return hashlib.sha256(framed.encode()).hexdigest()


def _backfill_legacy_corpus(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "corpus_documents"):
        return
    documents = conn.execute(
        """SELECT document_id, workspace_id, normalized_sha256, title, media_type,
        normalized_text, created_at FROM corpus_documents ORDER BY workspace_id, document_id"""
    ).fetchall()
    has_origins = _table_exists(conn, "corpus_origins")
    for document in documents:
        artifact_key = f"legacy:{document['document_id']}"
        source_id: str | None = None
        source_locator = f"legacy-document:{document['document_id']}"
        if has_origins:
            origin = conn.execute(
                """SELECT source_id, locator FROM corpus_origins WHERE document_id=?
                ORDER BY observed_at, source_id, locator LIMIT 1""",
                (document["document_id"],),
            ).fetchone()
            if origin is not None:
                source_id = origin["source_id"]
                source_locator = origin["locator"]

        conn.execute(
            """INSERT INTO knowledge_artifacts(
                workspace_id, artifact_key, current_version, visibility, created_at, updated_at
            ) VALUES (?, ?, 1, 'workspace', ?, ?)
            ON CONFLICT(workspace_id, artifact_key) DO NOTHING""",
            (
                document["workspace_id"],
                artifact_key,
                document["created_at"],
                document["created_at"],
            ),
        )
        conn.execute(
            """INSERT INTO knowledge_versions(
                workspace_id, artifact_key, version, normalized_sha256, raw_sha256,
                title, media_type, source_id, source_locator, parser_name, parser_version,
                normalization_version, chunker_version, chunk_max_chars, chunk_overlap_chars,
                approved_by, normalized_text, created_at
            ) VALUES (?, ?, 1, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, artifact_key, version) DO NOTHING""",
            (
                document["workspace_id"],
                artifact_key,
                document["normalized_sha256"],
                document["title"],
                document["media_type"],
                source_id,
                source_locator,
                "nika-legacy-corpus",
                "v9",
                "legacy-normalization-v9",
                "legacy-single-chunk-v1",
                max(1, len(document["normalized_text"])),
                0,
                "migration:legacy-corpus-v9",
                document["normalized_text"],
                document["created_at"],
            ),
        )
        text = document["normalized_text"]
        if not text:
            continue
        chunk_id = _legacy_chunk_id(document["workspace_id"], artifact_key, text)
        conn.execute(
            """INSERT INTO knowledge_chunks(
                chunk_id, workspace_id, artifact_key, version, ordinal,
                start_char, end_char, chunk_sha256, text
            ) VALUES (?, ?, ?, 1, 0, 0, ?, ?, ?)
            ON CONFLICT(chunk_id) DO NOTHING""",
            (
                chunk_id,
                document["workspace_id"],
                artifact_key,
                len(text),
                hashlib.sha256(text.encode()).hexdigest(),
                text,
            ),
        )
        exists = conn.execute(
            "SELECT COUNT(*) AS count FROM knowledge_fts WHERE chunk_id=?",
            (chunk_id,),
        ).fetchone()
        if int(exists["count"] or 0) == 0:
            conn.execute(
                """INSERT INTO knowledge_fts(
                    workspace_id, artifact_key, version, ordinal, chunk_id, title, body
                ) VALUES (?, ?, '1', '0', ?, ?, ?)""",
                (
                    document["workspace_id"],
                    artifact_key,
                    chunk_id,
                    document["title"],
                    text,
                ),
            )


def _rebuild_current_fts(conn: sqlite3.Connection) -> None:
    artifact_count = int(
        conn.execute("SELECT COUNT(*) FROM knowledge_artifacts").fetchone()[0]
    )
    current_version_count = int(
        conn.execute(
            """SELECT COUNT(*) FROM knowledge_artifacts AS a
            JOIN knowledge_versions AS v
              ON v.workspace_id=a.workspace_id
             AND v.artifact_key=a.artifact_key
             AND v.version=a.current_version"""
        ).fetchone()[0]
    )
    if current_version_count != artifact_count:
        raise RuntimeError("knowledge v3 migration found a missing current version")

    conn.execute("DELETE FROM knowledge_fts")
    conn.execute(
        """INSERT INTO knowledge_fts(
            workspace_id, artifact_key, version, ordinal, chunk_id, title, body
        )
        SELECT
            a.workspace_id, a.artifact_key, CAST(a.current_version AS TEXT),
            CAST(c.ordinal AS TEXT), c.chunk_id, v.title, c.text
        FROM knowledge_artifacts AS a
        JOIN knowledge_versions AS v
          ON v.workspace_id=a.workspace_id
         AND v.artifact_key=a.artifact_key
         AND v.version=a.current_version
        JOIN knowledge_chunks AS c
          ON c.workspace_id=a.workspace_id
         AND c.artifact_key=a.artifact_key
         AND c.version=a.current_version
        ORDER BY a.workspace_id, a.artifact_key, c.ordinal"""
    )


def initialize_knowledge_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute(
        "SELECT MAX(version) AS version FROM knowledge_schema_migrations"
    ).fetchone()
    current = int(row["version"] or 0)
    if current > KNOWLEDGE_SCHEMA_VERSION:
        raise RuntimeError(
            "knowledge database schema "
            f"{current} is newer than supported schema {KNOWLEDGE_SCHEMA_VERSION}"
        )
    for version in range(current + 1, KNOWLEDGE_SCHEMA_VERSION + 1):
        if version == 1:
            for statement in KNOWLEDGE_MIGRATION_1:
                conn.execute(statement)
        elif version == 2:
            _backfill_legacy_corpus(conn)
        elif version == 3:
            _rebuild_current_fts(conn)
        else:
            raise RuntimeError(f"missing knowledge migration {version}")
        conn.execute(
            "INSERT INTO knowledge_schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )
