from __future__ import annotations

TRAINING_SCALE_PROGRESSION_SCHEMA_VERSION = 1

TRAINING_SCALE_PROGRESSION_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS training_scale_progression_authority (
            proof_sha256 TEXT PRIMARY KEY,
            proof_json TEXT NOT NULL UNIQUE,
            plan_sha256 TEXT NOT NULL,
            tier_index INTEGER NOT NULL CHECK(tier_index >= 0),
            authorization_sha256 TEXT NOT NULL,
            job_id TEXT NOT NULL,
            candidate_artifact_ref TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL,
            comparison_evidence_sha256 TEXT NOT NULL,
            evaluation_set_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_training_scale_progression_candidate
            ON training_scale_progression_authority(
                plan_sha256,
                tier_index,
                candidate_artifact_ref,
                candidate_sha256
            )""",
    ),
}
