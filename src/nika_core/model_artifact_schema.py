from __future__ import annotations

MODEL_ARTIFACT_SCHEMA_VERSION = 1

MODEL_ARTIFACT_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS model_artifacts (
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            descriptor_json TEXT NOT NULL,
            descriptor_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(provider_id, model_id)
        )""",
    ),
}
