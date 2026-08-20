from __future__ import annotations

RESEARCH_DELTA_MIGRATION_13: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS research_profile_run_history (
        task_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL CHECK(profile_version > 0),
        source_set_id TEXT NOT NULL,
        source_set_version INTEGER NOT NULL CHECK(source_set_version > 0),
        result_set_id TEXT NOT NULL UNIQUE,
        previous_result_set_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(task_id),
        FOREIGN KEY(result_set_id) REFERENCES research_result_sets(result_set_id),
        FOREIGN KEY(previous_result_set_id) REFERENCES research_result_sets(result_set_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_research_profile_run_series ON research_profile_run_history(series_id, created_at DESC, task_id)",
    """CREATE TABLE IF NOT EXISTS research_profile_delta_items (
        task_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        change_kind TEXT NOT NULL CHECK(change_kind IN ('new', 'changed')),
        document_id TEXT NOT NULL,
        previous_document_id TEXT,
        PRIMARY KEY(task_id, ordinal),
        UNIQUE(task_id, document_id),
        FOREIGN KEY(task_id) REFERENCES research_profile_run_history(task_id) ON DELETE CASCADE,
        FOREIGN KEY(document_id) REFERENCES corpus_documents(document_id),
        FOREIGN KEY(previous_document_id) REFERENCES corpus_documents(document_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_research_profile_delta_task ON research_profile_delta_items(task_id, change_kind, ordinal)",
)
