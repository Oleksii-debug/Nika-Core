from __future__ import annotations

from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    MediaAsset,
    MediaSource,
    MediaVersion,
    OptionalComponent,
    ProcessingJob,
    ProcessingState,
    TextRevision,
)


class MediaRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put_source(self, source: MediaSource) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO media_sources(source_id, source_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_json = excluded.source_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source.source_id,
                    source.model_dump_json(),
                    source.created_at.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_source(self, source_id: str) -> MediaSource:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT source_json FROM media_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown media source: {source_id}")
        return MediaSource.model_validate_json(row["source_json"])

    def put_version(self, version: MediaVersion) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO media_versions(
                    version_id, source_id, version_json, observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(version_id) DO UPDATE SET version_json = excluded.version_json
                """,
                (
                    version.version_id,
                    version.source_id,
                    version.model_dump_json(),
                    version.observed_at.isoformat(),
                ),
            )

    def get_version(self, version_id: str) -> MediaVersion:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT version_json FROM media_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown media version: {version_id}")
        return MediaVersion.model_validate_json(row["version_json"])

    def put_asset(self, asset: MediaAsset) -> None:
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT asset_json FROM media_assets WHERE asset_id = ?",
                (asset.asset_id,),
            ).fetchone()
            if existing is not None:
                current = MediaAsset.model_validate_json(existing["asset_json"])
                if current != asset:
                    raise ValueError("media assets are immutable once registered")
                return
            conn.execute(
                """INSERT INTO media_assets(
                    asset_id, version_id, kind, sha256, asset_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    asset.version_id,
                    asset.kind.value,
                    asset.sha256,
                    asset.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_assets(self, version_id: str) -> tuple[MediaAsset, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT asset_json FROM media_assets WHERE version_id = ? ORDER BY created_at, asset_id",
                (version_id,),
            ).fetchall()
        return tuple(MediaAsset.model_validate_json(row["asset_json"]) for row in rows)

    def put_job(self, job: ProcessingJob) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO media_processing_jobs(
                    job_id, source_id, version_id, stage, state, job_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    version_id = excluded.version_id,
                    stage = excluded.stage,
                    state = excluded.state,
                    job_json = excluded.job_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.job_id,
                    job.source_id,
                    job.version_id,
                    job.stage,
                    job.state.value,
                    job.model_dump_json(),
                    job.updated_at.isoformat(),
                ),
            )

    def get_job(self, job_id: str) -> ProcessingJob:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT job_json FROM media_processing_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown media processing job: {job_id}")
        return ProcessingJob.model_validate_json(row["job_json"])

    def recoverable_jobs(self) -> tuple[ProcessingJob, ...]:
        states = (
            ProcessingState.PENDING.value,
            ProcessingState.RUNNING.value,
            ProcessingState.BLOCKED.value,
        )
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT job_json FROM media_processing_jobs
                WHERE state IN (?, ?, ?) ORDER BY updated_at, job_id""",
                states,
            ).fetchall()
        recovered: list[ProcessingJob] = []
        for row in rows:
            job = ProcessingJob.model_validate_json(row["job_json"])
            if job.state == ProcessingState.RUNNING:
                job = job.model_copy(
                    update={
                        "state": ProcessingState.BLOCKED,
                        "last_error_code": "restart_reconciliation_required",
                        "last_error_message": (
                            "Process restarted while media job was running; resume from durable checkpoint."
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
                self.put_job(job)
            recovered.append(job)
        return tuple(recovered)

    def put_component(self, component: OptionalComponent) -> None:
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO media_optional_components(component_id, component_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(component_id) DO UPDATE SET
                    component_json = excluded.component_json,
                    updated_at = excluded.updated_at
                """,
                (
                    component.component_id,
                    component.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_component(self, component_id: str) -> OptionalComponent | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT component_json FROM media_optional_components WHERE component_id = ?",
                (component_id,),
            ).fetchone()
        if row is None:
            return None
        return OptionalComponent.model_validate_json(row["component_json"])

    def append_revision(self, revision: TextRevision) -> None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(ordinal) AS ordinal FROM media_text_revisions WHERE artifact_id = ?",
                (revision.artifact_id,),
            ).fetchone()
            expected = int(row["ordinal"] + 1) if row["ordinal"] is not None else 0
            if revision.ordinal != expected:
                raise ValueError(f"revision ordinal must be {expected}")
            conn.execute(
                """INSERT INTO media_text_revisions(
                    revision_id, artifact_id, ordinal, parent_revision_id, revision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.revision_id,
                    revision.artifact_id,
                    revision.ordinal,
                    revision.parent_revision_id,
                    revision.model_dump_json(),
                    revision.created_at.isoformat(),
                ),
            )

    def revisions(self, artifact_id: str) -> tuple[TextRevision, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT revision_json FROM media_text_revisions WHERE artifact_id = ? ORDER BY ordinal",
                (artifact_id,),
            ).fetchall()
        return tuple(TextRevision.model_validate_json(row["revision_json"]) for row in rows)
