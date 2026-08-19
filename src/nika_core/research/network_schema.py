from __future__ import annotations

RESEARCH_NETWORK_MIGRATION_11: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS research_http_sources (
        source_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        url TEXT NOT NULL,
        final_url TEXT,
        etag TEXT,
        last_modified TEXT,
        current_raw_sha256 TEXT,
        freshness TEXT NOT NULL CHECK(freshness IN (
            'unknown','current','stale','removed','blocked','error'
        )),
        last_attempt_at TEXT,
        last_success_at TEXT,
        last_status_code INTEGER,
        last_error_code TEXT,
        last_error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES research_workspaces(workspace_id),
        UNIQUE(workspace_id, url)
    )""",
    """CREATE TABLE IF NOT EXISTS research_http_attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT,
        source_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
        disposition TEXT NOT NULL CHECK(disposition IN (
            'changed','not_modified','unchanged','dynamic_required','removed',
            'blocked','unsupported','failed'
        )),
        status_code INTEGER,
        requested_url TEXT NOT NULL,
        final_url TEXT NOT NULL,
        error_code TEXT,
        error_message TEXT NOT NULL,
        retryable INTEGER NOT NULL CHECK(retryable IN (0, 1)),
        observed_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(task_id),
        FOREIGN KEY(source_id) REFERENCES research_http_sources(source_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_http_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL,
        media_type TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        extraction_id TEXT,
        document_id TEXT,
        observed_at TEXT NOT NULL,
        FOREIGN KEY(source_id) REFERENCES research_http_sources(source_id),
        FOREIGN KEY(artifact_id) REFERENCES corpus_artifacts(artifact_id),
        FOREIGN KEY(extraction_id) REFERENCES corpus_extractions(extraction_id),
        FOREIGN KEY(document_id) REFERENCES corpus_documents(document_id),
        UNIQUE(source_id, raw_sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS corpus_http_origins (
        document_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        locator TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY(document_id, source_id, snapshot_id),
        FOREIGN KEY(document_id) REFERENCES corpus_documents(document_id),
        FOREIGN KEY(source_id) REFERENCES research_http_sources(source_id),
        FOREIGN KEY(snapshot_id) REFERENCES research_http_snapshots(snapshot_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_result_sets (
        result_set_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        query TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(workspace_id) REFERENCES research_workspaces(workspace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_result_items (
        result_set_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        document_id TEXT NOT NULL,
        title TEXT NOT NULL,
        snippet TEXT NOT NULL,
        rank REAL NOT NULL,
        why_matched TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(result_set_id, ordinal),
        FOREIGN KEY(result_set_id) REFERENCES research_result_sets(result_set_id),
        FOREIGN KEY(document_id) REFERENCES corpus_documents(document_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_http_sources_workspace ON research_http_sources(workspace_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_http_attempts_source ON research_http_attempts(source_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_http_attempts_task ON research_http_attempts(task_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_http_snapshots_source ON research_http_snapshots(source_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_corpus_http_origins_doc ON corpus_http_origins(document_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_result_sets_workspace ON research_result_sets(workspace_id, created_at)",
)
