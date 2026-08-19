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
    ProvenanceChain,
    StructuredMediaArtifact,
    TextRevision,
)
from nika_core.media.privacy import redact_mapping, redact_text


class MediaRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put_source(self, source: MediaSource) -> None:
        safe_source = source.model_copy(update={"locator": redact_text(source.locator)})
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO media_sources(source_id, source_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_json = excluded.source_json,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_source.source_id,
                    safe_source.model_dump_json(),
                    safe_source.created_at.isoformat(),
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
            existing = conn.execute(
                "SELECT version_json FROM media_versions WHERE version_id = ?",
                (version.version_id,),
            ).fetchone()
            if existing is not None:
                current = MediaVersion.model_validate_json(existing["version_json"])
                if current != version:
                    raise ValueError("media versions are immutable once registered")
                return
            conn.execute(
                """INSERT INTO media_versions(
                    version_id, source_id, version_json, observed_at
                ) VALUES (?, ?, ?, ?)
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
        safe_job = job.model_copy(
            update={
                "checkpoint_json": redact_mapping(job.checkpoint_json),
                "last_error_message": (
                    redact_text(job.last_error_message) if job.last_error_message is not None else None
                ),
            }
        )
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
                    safe_job.job_id,
                    safe_job.source_id,
                    safe_job.version_id,
                    safe_job.stage,
                    safe_job.state.value,
                    safe_job.model_dump_json(),
                    safe_job.updated_at.isoformat(),
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

    def put_artifact(self, artifact: StructuredMediaArtifact) -> None:
        if any(revision.artifact_id != artifact.artifact_id for revision in artifact.revisions):
            raise ValueError("artifact revisions must belong to the structured media artifact")
        safe_events = tuple(
            event.model_copy(update={"details": redact_mapping(event.details)})
            for event in artifact.provenance.events
        )
        safe_source = artifact.source.model_copy(
            update={"locator": redact_text(artifact.source.locator)}
        )
        safe_artifact = artifact.model_copy(
            update={
                "source": safe_source,
                "provenance": ProvenanceChain(events=safe_events),
            }
        )
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT artifact_json FROM media_structured_artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                current = StructuredMediaArtifact.model_validate_json(existing["artifact_json"])
                if current != safe_artifact:
                    raise ValueError("structured media artifacts are immutable once registered")
                return
            conn.execute(
                """INSERT INTO media_structured_artifacts(
                    artifact_id, version_id, artifact_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    safe_artifact.artifact_id,
                    safe_artifact.version_id,
                    safe_artifact.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_artifact(self, artifact_id: str) -> StructuredMediaArtifact:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT artifact_json FROM media_structured_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown structured media artifact: {artifact_id}")
        return StructuredMediaArtifact.model_validate_json(row["artifact_json"])

    def append_revision(self, revision: TextRevision) -> None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT MAX(ordinal) AS ordinal FROM media_text_revisions WHERE artifact_id = ?",
                (revision.artifact_id,),
            ).fetchone()
            expected = int(row["ordinal"] + 1) if row["ordinal"] is not None else 0
            if revision.ordinal != expected:
                raise ValueError(f"revision ordinal must be {expected}")
            if revision.parent_revision_id is not None:
                parent = conn.execute(
                    """SELECT artifact_id, ordinal FROM media_text_revisions
                    WHERE revision_id = ?""",
                    (revision.parent_revision_id,),
                ).fetchone()
                if parent is None:
                    raise ValueError("parent revision does not exist")
                if parent["artifact_id"] != revision.artifact_id or int(parent["ordinal"]) != expected - 1:
                    raise ValueError("parent revision must be the previous revision of this artifact")
            elif expected != 0:
                raise ValueError("non-initial revision requires parent_revision_id")
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
