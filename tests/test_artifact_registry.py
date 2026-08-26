from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.artifacts import (
    ARTIFACT_REGISTRY_SCHEMA_VERSION,
    ArtifactConflictError,
    ArtifactLocationKind,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactVerificationState,
    initialize_artifact_registry_schema,
)
from nika_core.data.sqlite import SQLiteStore


def _registry(
    db_path: Path,
    *,
    now: datetime = datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
) -> ArtifactRegistry:
    store = SQLiteStore(db_path)
    return ArtifactRegistry.from_store(store, clock=lambda: now)


def test_schema_migration_is_idempotent_and_reports_owned_version(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    initialize_artifact_registry_schema(store)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM artifact_registry_schema_migrations"
        ).fetchone()
        assert int(row["version"]) == ARTIFACT_REGISTRY_SCHEMA_VERSION


def test_schema_fails_closed_when_database_is_newer(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO artifact_registry_schema_migrations(version, applied_at) VALUES (?, ?)",
            (ARTIFACT_REGISTRY_SCHEMA_VERSION + 1, datetime.now(UTC).isoformat()),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        initialize_artifact_registry_schema(store)


def test_register_file_is_durable_and_idempotent_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    source = tmp_path / "дані з пробілами.txt"
    source.write_text("artifact payload", encoding="utf-8")

    first = _registry(db_path).register_file(
        workspace_id="workspace-a",
        idempotency_key="report-final",
        path=source,
        kind="report",
        producer_type="task",
        producer_id="task-7",
    )
    restarted = _registry(db_path)
    second = restarted.register_file(
        workspace_id="workspace-a",
        idempotency_key="report-final",
        path=source,
        kind="report",
        producer_type="task",
        producer_id="task-7",
    )

    assert second == first
    assert restarted.get(first.artifact_id) == first
    assert first.location_kind == ArtifactLocationKind.LOCAL_FILE
    assert first.locator == str(source.resolve())
    assert first.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


def test_idempotency_key_rejects_changed_immutable_metadata(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    source = tmp_path / "result.bin"
    source.write_bytes(b"first")
    registry.register_file(
        workspace_id="workspace-a",
        idempotency_key="same-effect",
        path=source,
        kind="result",
    )
    source.write_bytes(b"second")

    with pytest.raises(ArtifactConflictError, match="different immutable metadata"):
        registry.register_file(
            workspace_id="workspace-a",
            idempotency_key="same-effect",
            path=source,
            kind="result",
        )


def test_concurrent_replay_converges_to_one_record(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    source = tmp_path / "payload.txt"
    source.write_text("stable", encoding="utf-8")
    _registry(db_path)

    def register() -> str:
        return _registry(db_path).register_file(
            workspace_id="workspace-concurrent",
            idempotency_key="one-effect",
            path=source,
            kind="result",
        ).artifact_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        artifact_ids = tuple(executor.map(lambda _: register(), range(8)))

    assert len(set(artifact_ids)) == 1
    records = _registry(db_path).list(workspace_id="workspace-concurrent")
    assert len(records) == 1


def test_verify_records_success_tamper_and_missing_history(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    source = tmp_path / "evidence.txt"
    source.write_text("trusted", encoding="utf-8")
    record = registry.register_file(
        workspace_id="workspace-a",
        idempotency_key="evidence",
        path=source,
        kind="evidence",
    )

    assert registry.verify(record.artifact_id).state == ArtifactVerificationState.VERIFIED

    source.write_text("tampered", encoding="utf-8")
    assert registry.verify(record.artifact_id).state == ArtifactVerificationState.MISMATCH

    source.unlink()
    assert registry.verify(record.artifact_id).state == ArtifactVerificationState.MISSING

    assert tuple(item.state for item in registry.verification_history(record.artifact_id)) == (
        ArtifactVerificationState.VERIFIED,
        ArtifactVerificationState.MISMATCH,
        ArtifactVerificationState.MISSING,
    )


def test_opaque_reference_is_registered_without_claiming_byte_verification(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    record = registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="blob-handoff",
        reference="blob:workspace-a/ab/abcdef",
        sha256="a" * 64,
        size_bytes=42,
        kind="research_blob",
    )

    verification = registry.verify(record.artifact_id)
    assert record.location_kind == ArtifactLocationKind.OPAQUE_REFERENCE
    assert verification.state == ArtifactVerificationState.UNAVAILABLE
    assert verification.actual_sha256 is None


def test_list_filters_workspace_kind_and_producer(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    for index, producer in enumerate(("agent-a", "agent-b", "agent-a")):
        registry.register_reference(
            workspace_id="workspace-a",
            idempotency_key=f"item-{index}",
            reference=f"blob:item-{index}",
            sha256=f"{index + 1:064x}",
            size_bytes=index,
            kind="report" if index < 2 else "log",
            producer_id=producer,
        )
    registry.register_reference(
        workspace_id="workspace-b",
        idempotency_key="other",
        reference="blob:other",
        sha256="f" * 64,
        size_bytes=1,
        kind="report",
        producer_id="agent-a",
    )

    reports = registry.list(workspace_id="workspace-a", kind="report")
    assert len(reports) == 2
    assert {record.producer_id for record in reports} == {"agent-a", "agent-b"}

    agent_a = registry.list(workspace_id="workspace-a", producer_id="agent-a")
    assert len(agent_a) == 2
    assert all(record.producer_id == "agent-a" for record in agent_a)


def test_secret_like_metadata_and_locators_are_rejected() -> None:
    common = dict(
        artifact_id="a" * 64,
        idempotency_key="idempotent",
        workspace_id="workspace",
        kind="report",
        location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
        sha256="b" * 64,
        size_bytes=1,
    )
    with pytest.raises(ValidationError, match="credential material"):
        ArtifactRecord(**common, locator="https://example.test/file?token=secret")
    with pytest.raises(ValidationError, match="secret material"):
        ArtifactRecord(**common, locator="blob:safe", metadata={"password": "secret"})


def test_find_by_digest_and_producer_filter_apply_before_limit(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    digest = "c" * 64
    registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="first",
        reference="blob:first",
        sha256=digest,
        size_bytes=1,
        kind="report",
        producer_id="other",
    )
    expected = registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="second",
        reference="blob:second",
        sha256=digest,
        size_bytes=1,
        kind="report",
        producer_id="target",
    )

    assert registry.list(workspace_id="workspace-a", producer_id="target", limit=1) == (expected,)
    assert registry.find_by_sha256(digest, workspace_id="workspace-a") == (
        registry.get(registry.list(workspace_id="workspace-a")[0].artifact_id),
        expected,
    )


def test_naive_clock_is_rejected_instead_of_assuming_host_timezone(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(
        store,
        clock=lambda: datetime(2026, 8, 26, 20, 0),
    )

    with pytest.raises(RuntimeError, match="timezone-aware"):
        registry.register_reference(
            workspace_id="workspace-a",
            idempotency_key="clock",
            reference="blob:clock",
            sha256="d" * 64,
            size_bytes=1,
            kind="evidence",
        )
