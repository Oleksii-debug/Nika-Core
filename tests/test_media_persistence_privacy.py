from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import (
    MediaSource,
    MediaSourceKind,
    MediaVersion,
    ProcessingJob,
    ProvenanceChain,
    ProvenanceEvent,
    StructuredMediaArtifact,
)
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import initialize_media_schema


def repository(tmp_path: Path) -> MediaRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    return MediaRepository(store)


def identity(repo: MediaRepository) -> tuple[MediaSource, MediaVersion]:
    source = MediaSource(
        source_id="remote-1",
        kind=MediaSourceKind.REMOTE_MEDIA,
        locator="https://example.test/watch?id=1&token=secret-token",
    )
    version = MediaVersion(
        version_id="version-1",
        source_id=source.source_id,
        metadata_sha256="a" * 64,
    )
    repo.put_source(source)
    repo.put_version(version)
    return repo.get_source(source.source_id), version


def test_media_version_is_immutable_after_registration(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    _source, version = identity(repo)
    with pytest.raises(ValueError, match="versions are immutable"):
        repo.put_version(version.model_copy(update={"title": "mutated"}))
    assert repo.get_version(version.version_id).title == ""


def test_repository_redacts_source_job_and_provenance_before_persistence(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    source, version = identity(repo)
    assert "secret-token" not in source.locator
    repo.put_job(
        ProcessingJob(
            job_id="job-1",
            source_id=source.source_id,
            version_id=version.version_id,
            stage="metadata",
            checkpoint_json={"token": "secret-checkpoint", "cursor": "safe"},
            last_error_message="failed at https://x.test/a?signature=secret-signature",
        )
    )
    stored_job = repo.get_job("job-1")
    assert stored_job.checkpoint_json["token"] == "[REDACTED]"
    assert "secret-signature" not in (stored_job.last_error_message or "")

    artifact = StructuredMediaArtifact(
        artifact_id="artifact-1",
        version_id=version.version_id,
        source=source,
        version=version,
        provenance=ProvenanceChain(
            events=(
                ProvenanceEvent(
                    sequence=0,
                    event_type="media.discovered",
                    actor="dev05",
                    details={"token": "secret-provenance", "mode": "metadata"},
                ),
            )
        ),
    )
    repo.put_artifact(artifact)
    stored = repo.get_artifact("artifact-1")
    assert stored.provenance.events[0].details["token"] == "[REDACTED]"
    assert stored.provenance.events[0].details["mode"] == "metadata"


def test_structured_artifact_is_immutable_after_registration(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    source, version = identity(repo)
    artifact = StructuredMediaArtifact(
        artifact_id="artifact-1",
        version_id=version.version_id,
        source=source,
        version=version,
    )
    repo.put_artifact(artifact)
    with pytest.raises(ValueError, match="artifacts are immutable"):
        repo.put_artifact(
            artifact.model_copy(
                update={
                    "provenance": ProvenanceChain(
                        events=(
                            ProvenanceEvent(
                                sequence=0,
                                event_type="changed",
                                actor="dev05",
                            ),
                        )
                    )
                }
            )
        )
