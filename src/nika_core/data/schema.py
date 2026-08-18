from __future__ import annotations

SCHEMA_VERSION = 5

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
    3: (
        """CREATE TABLE IF NOT EXISTS runtime_sessions (
            task_id TEXT PRIMARY KEY,
            runtime_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            resume_token TEXT NOT NULL,
            outcome TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id),
            UNIQUE(runtime_id, thread_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_runtime_sessions_outcome ON runtime_sessions(outcome)",
        """CREATE TABLE IF NOT EXISTS idempotency_records (
            operation_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_idempotency_task ON idempotency_records(task_id, status)",
    ),
    4: (
        """CREATE TABLE IF NOT EXISTS memory_records (
            scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            user_approved INTEGER NOT NULL CHECK(user_approved IN (0, 1)),
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope, owner_id, namespace, memory_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_expiry ON memory_records(expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(scope, owner_id, namespace)",
        """CREATE TABLE IF NOT EXISTS scheduled_jobs (
            job_id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL,
            trigger_kind TEXT NOT NULL,
            trigger_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            coalesce INTEGER NOT NULL CHECK(coalesce IN (0, 1)),
            max_instances INTEGER NOT NULL CHECK(max_instances > 0),
            misfire_grace_seconds INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_enabled ON scheduled_jobs(enabled)",
        """CREATE TABLE IF NOT EXISTS resource_budgets (
            scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            max_concurrent INTEGER NOT NULL CHECK(max_concurrent > 0),
            max_cpu_percent REAL,
            max_memory_percent REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope, owner_id)
        )""",
    ),
    5: (
        """CREATE TABLE IF NOT EXISTS agent_definitions (
            agent_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            definition_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'retired')),
            created_at TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY(agent_id, version)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agent_definitions_latest "
        "ON agent_definitions(agent_id, version DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_definitions_one_active "
        "ON agent_definitions(agent_id) WHERE status = 'active'",
    ),
}
