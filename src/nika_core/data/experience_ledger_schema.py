from __future__ import annotations

EXPERIENCE_LEDGER_SCHEMA_VERSION = 1

EXPERIENCE_LEDGER_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS continuity_experience_events (
            event_key TEXT PRIMARY KEY,
            task_id TEXT,
            kind TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            attempt INTEGER,
            delay_seconds REAL,
            clock_jump_seconds REAL,
            fingerprint TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_continuity_experience_task_time
        ON continuity_experience_events(task_id, occurred_at)""",
    ),
}
