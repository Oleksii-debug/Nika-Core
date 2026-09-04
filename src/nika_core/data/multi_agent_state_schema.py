from __future__ import annotations

# Supplemental migration family for the V0.1 multi-agent member-state contract.
# It is applied only by the canonical SQLiteStore initializer after the core
# schema migrations, so it does not create a second persistence authority.
MULTI_AGENT_STATE_SCHEMA_VERSION = 1

MULTI_AGENT_STATE_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE multi_agent_members_v01_state (
            team_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            parent_id TEXT,
            depth INTEGER NOT NULL CHECK(depth >= 0),
            agent_id TEXT NOT NULL,
            agent_version INTEGER NOT NULL CHECK(agent_version > 0),
            thread_id TEXT NOT NULL,
            tool_grants_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'spawned', 'running', 'waiting_approval', 'paused',
                'completed', 'failed', 'cancelled'
            )),
            resume_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(team_id, member_id),
            FOREIGN KEY(team_id) REFERENCES multi_agent_teams(team_id)
        )""",
        """INSERT INTO multi_agent_members_v01_state(
            team_id, member_id, parent_id, depth, agent_id, agent_version,
            thread_id, tool_grants_json, state, resume_token, created_at, updated_at
        )
        SELECT
            team_id, member_id, parent_id, depth, agent_id, agent_version,
            thread_id, tool_grants_json, state, resume_token, created_at, updated_at
        FROM multi_agent_members""",
        "DROP TABLE multi_agent_members",
        "ALTER TABLE multi_agent_members_v01_state RENAME TO multi_agent_members",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_parent ON multi_agent_members(team_id, parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_state ON multi_agent_members(team_id, state)",
    ),
}
