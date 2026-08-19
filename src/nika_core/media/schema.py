from __future__ import annotations

from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

MEDIA_SCHEMA_VERSION = 3

_MEDIA_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS media_sources (
            source_id TEXT PRIMARY KEY,
            source_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS media_versions (
            version_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            version_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(source_id) REFERENCES media_sources(source_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_versions_source ON media_versions(source_id, observed_at)",
        """CREATE TABLE IF NOT EXISTS media_assets (
            asset_id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            asset_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(version_id) REFERENCES media_versions(version_id),
            UNIQUE(version_id, kind, sha256)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_assets_version ON media_assets(version_id, created_at)",
        """CREATE TABLE IF NOT EXISTS media_processing_jobs (
            job_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            version_id TEXT,
            stage TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','running','blocked','completed','failed','cancelled')),
            job_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_id) REFERENCES media_sources(source_id),
            FOREIGN KEY(version_id) REFERENCES media_versions(version_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_jobs_state ON media_processing_jobs(state, updated_at)",
        """CREATE TABLE IF NOT EXISTS media_optional_components (
            component_id TEXT PRIMARY KEY,
            component_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS media_structured_artifacts (
            artifact_id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(version_id) REFERENCES media_versions(version_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_artifacts_version ON media_structured_artifacts(version_id)",
        """CREATE TABLE IF NOT EXISTS media_text_revisions (
            revision_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            parent_revision_id TEXT,
            revision_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(artifact_id, ordinal),
            FOREIGN KEY(parent_revision_id) REFERENCES media_text_revisions(revision_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_revisions_artifact ON media_text_revisions(artifact_id, ordinal)",
    ),
    2: (
        """CREATE TABLE IF NOT EXISTS media_transcription_chunks (
            chunk_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','cancelled')),
            chunk_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, ordinal),
            FOREIGN KEY(job_id) REFERENCES media_processing_jobs(job_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_transcription_job_state ON media_transcription_chunks(job_id, state, ordinal)",
    ),
    3: (
        """CREATE TABLE IF NOT EXISTS media_ocr_pages (
            page_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','cancelled')),
            page_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, ordinal),
            FOREIGN KEY(job_id) REFERENCES media_processing_jobs(job_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_media_ocr_job_state ON media_ocr_pages(job_id, state, ordinal)",
    ),
}


def initialize_media_schema(store: SQLiteStore) -> None:
    """Apply DEV05-owned ordered migrations inside Nika's canonical SQLite database."""
    with store.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS media_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        row = conn.execute("SELECT MAX(version) AS version FROM media_schema_migrations").fetchone()
        current = int(row["version"] or 0)
        if current > MEDIA_SCHEMA_VERSION:
            raise RuntimeError(
                f"media schema {current} is newer than supported schema {MEDIA_SCHEMA_VERSION}"
            )
        for version in range(current + 1, MEDIA_SCHEMA_VERSION + 1):
            statements = _MEDIA_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing media migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO media_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
