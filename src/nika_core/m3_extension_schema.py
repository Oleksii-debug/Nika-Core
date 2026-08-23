from __future__ import annotations

M3_EXTENSION_SCHEMA_VERSION = 2

M3_EXTENSION_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """ALTER TABLE resource_budgets
        ADD COLUMN max_disk_percent REAL
        CHECK(max_disk_percent IS NULL OR (max_disk_percent > 0 AND max_disk_percent <= 100))""",
        """ALTER TABLE resource_budgets
        ADD COLUMN max_gpu_percent REAL
        CHECK(max_gpu_percent IS NULL OR (max_gpu_percent > 0 AND max_gpu_percent <= 100))""",
        """ALTER TABLE resource_budgets
        ADD COLUMN max_process_memory_bytes INTEGER
        CHECK(max_process_memory_bytes IS NULL OR max_process_memory_bytes > 0)""",
        """CREATE TABLE scheduled_job_bindings (
            job_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            product_project_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES scheduled_jobs(job_id) ON DELETE CASCADE,
            UNIQUE(scope, owner_id, dedup_key)
        )""",
        """CREATE INDEX idx_scheduled_job_bindings_owner
        ON scheduled_job_bindings(scope, owner_id, dedup_key)""",
        """CREATE TABLE resource_requests (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            product_project_id TEXT,
            state TEXT NOT NULL
                CHECK(state IN (
                    'waiting',
                    'granted',
                    'released',
                    'cancelled',
                    'released_after_restart'
                )),
            lease_owner_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, owner_id, request_id)
        )""",
        """CREATE INDEX idx_resource_requests_queue
        ON resource_requests(scope, owner_id, state, sequence)""",
        """CREATE INDEX idx_resource_requests_lease_owner
        ON resource_requests(lease_owner_id, state)""",
    ),
    2: (
        """ALTER TABLE resource_requests
        ADD COLUMN lease_owner_process_id INTEGER""",
        """ALTER TABLE resource_requests
        ADD COLUMN lease_owner_started_at REAL""",
    ),
}
