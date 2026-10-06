from __future__ import annotations

RESEARCH_PROFILE_MIGRATION_12: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS research_source_sets (
        source_set_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_set_id, version),
        FOREIGN KEY(workspace_id) REFERENCES research_workspaces(workspace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS research_source_set_members (
        source_set_id TEXT NOT NULL,
        source_set_version INTEGER NOT NULL CHECK(source_set_version > 0),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        source_id TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('local_file', 'http')),
        PRIMARY KEY(source_set_id, source_set_version, ordinal),
        UNIQUE(source_set_id, source_set_version, source_id),
        FOREIGN KEY(source_set_id, source_set_version)
            REFERENCES research_source_sets(source_set_id, version)
            ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS research_profiles (
        profile_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        name TEXT NOT NULL,
        source_set_id TEXT NOT NULL,
        source_set_version INTEGER NOT NULL CHECK(source_set_version > 0),
        query_text TEXT NOT NULL,
        query_mode TEXT NOT NULL CHECK(query_mode IN ('literal', 'phrase')),
        filters_json TEXT NOT NULL,
        result_limit INTEGER NOT NULL CHECK(result_limit BETWEEN 1 AND 100),
        created_at TEXT NOT NULL,
        PRIMARY KEY(profile_id, version),
        FOREIGN KEY(workspace_id) REFERENCES research_workspaces(workspace_id),
        FOREIGN KEY(source_set_id, source_set_version)
            REFERENCES research_source_sets(source_set_id, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_research_source_sets_workspace ON research_source_sets(workspace_id, source_set_id, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_research_source_set_members_source ON research_source_set_members(source_id, source_kind)",
    "CREATE INDEX IF NOT EXISTS idx_research_profiles_workspace ON research_profiles(workspace_id, profile_id, version DESC)",
)
