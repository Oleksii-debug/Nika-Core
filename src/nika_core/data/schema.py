from __future__ import annotations

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            previous_state TEXT,
            new_state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            checksum_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )""",
    ),
    2: (
        """CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, version)
        )""",
        """CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            goal TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(agent_id, version)
        )""",
        """CREATE TABLE IF NOT EXISTS audit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS keymap_overrides (
            action_id TEXT PRIMARY KEY,
            binding TEXT,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agents_latest ON agents(agent_id, version DESC)",
        "CREATE INDEX IF NOT EXISTS idx_workspaces_latest ON workspaces(workspace_id, version DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, event_id)",
    ),
}
