from __future__ import annotations

PRODUCT_PROJECT_SCHEMA_VERSION = 3

PRODUCT_PROJECT_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS product_projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            current_spec_version INTEGER NOT NULL CHECK(current_spec_version > 0),
            row_version INTEGER NOT NULL CHECK(row_version >= 0),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS product_project_specs (
            project_id TEXT NOT NULL,
            spec_version INTEGER NOT NULL CHECK(spec_version > 0),
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(project_id, spec_version),
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        """CREATE TABLE IF NOT EXISTS product_project_idempotency (
            operation_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        """CREATE TABLE IF NOT EXISTS product_research_handoffs (
            project_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(project_id, package_id),
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        (
            "CREATE INDEX IF NOT EXISTS idx_product_projects_status "
            "ON product_projects(status, updated_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_product_project_specs_latest "
            "ON product_project_specs(project_id, spec_version DESC)"
        ),
    ),
    2: (
        """CREATE TABLE IF NOT EXISTS product_decisions (
            project_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            decision_version INTEGER NOT NULL CHECK(decision_version > 0),
            option_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('proposed', 'approved', 'rejected')),
            rationale TEXT NOT NULL,
            decided_by_ref TEXT NOT NULL,
            evidence_package_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(project_id, decision_id, decision_version),
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        """CREATE TABLE IF NOT EXISTS product_project_mutation_idempotency (
            operation_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_version INTEGER NOT NULL CHECK(entity_version > 0),
            input_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        (
            "CREATE INDEX IF NOT EXISTS idx_product_decisions_latest "
            "ON product_decisions(project_id, decision_id, decision_version DESC)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_product_decisions_option "
            "ON product_decisions(project_id, option_id, decision_version DESC)"
        ),
    ),
    3: (
        """CREATE TABLE IF NOT EXISTS product_project_spec_idempotency (
            operation_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            expected_row_version INTEGER NOT NULL CHECK(expected_row_version >= 0),
            previous_spec_version INTEGER NOT NULL CHECK(previous_spec_version > 0),
            result_spec_version INTEGER NOT NULL CHECK(result_spec_version > 1),
            result_row_version INTEGER NOT NULL CHECK(result_row_version > 0),
            input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint) = 64),
            spec_sha256 TEXT NOT NULL CHECK(length(spec_sha256) = 64),
            change_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, result_spec_version),
            FOREIGN KEY(project_id) REFERENCES product_projects(project_id)
        )""",
        (
            "CREATE INDEX IF NOT EXISTS idx_product_project_spec_idempotency_result "
            "ON product_project_spec_idempotency(project_id, result_row_version)"
        ),
    ),
}
