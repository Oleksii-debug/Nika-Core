from __future__ import annotations

SCHEMA_VERSION = 7

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
            required_approvals_json TEXT NOT NULL,
            highest_risk INTEGER NOT NULL CHECK(highest_risk BETWEEN 0 AND 4),
            status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'retired')),
            created_at TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY(agent_id, version)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agent_definitions_latest ON agent_definitions(agent_id, version DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_definitions_one_active ON agent_definitions(agent_id) WHERE status = 'active'",
    ),
    6: (
        """CREATE TABLE IF NOT EXISTS multi_agent_teams (
            team_id TEXT PRIMARY KEY,
            root_member_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active', 'cancelled', 'completed', 'failed')),
            quota_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS multi_agent_members (
            team_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            parent_id TEXT,
            depth INTEGER NOT NULL CHECK(depth >= 0),
            agent_id TEXT NOT NULL,
            agent_version INTEGER NOT NULL CHECK(agent_version > 0),
            thread_id TEXT NOT NULL,
            tool_grants_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('spawned', 'running', 'waiting_approval', 'completed', 'failed', 'cancelled')),
            resume_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(team_id, member_id),
            FOREIGN KEY(team_id) REFERENCES multi_agent_teams(team_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_parent ON multi_agent_members(team_id, parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_state ON multi_agent_members(team_id, state)",
        """CREATE TABLE IF NOT EXISTS multi_agent_handoffs (
            handoff_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('task', 'result', 'status', 'error')),
            correlation_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(team_id) REFERENCES multi_agent_teams(team_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_handoff_team ON multi_agent_handoffs(team_id, created_at)",
        """CREATE TABLE IF NOT EXISTS multi_agent_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(team_id) REFERENCES multi_agent_teams(team_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_result_team ON multi_agent_results(team_id, member_id)",
    ),
    7: (
        """CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY,
            definition_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'running', 'completed', 'promoted', 'rolled_back')),
            selected_candidate_id TEXT,
            previous_champion_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS experiment_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            replay_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id),
            UNIQUE(experiment_id, candidate_id, replay_id, metric)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_experiment_observations_run ON experiment_observations(experiment_id, observation_id)",
        """CREATE TABLE IF NOT EXISTS experiment_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            selected_candidate_id TEXT,
            previous_champion_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_experiment_events_run ON experiment_events(experiment_id, event_id)",
    ),
}
