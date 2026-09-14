from __future__ import annotations

import hashlib
import json
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
    ArtifactRegistryError,
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
    return ArtifactRegistry.from_store(
        store,
        clock=lambda: now,
        local_file_roots=(db_path.parent,),
    )


def test_schema_migration_is_idempotent_and_reports_owned_version(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    initialize_artifact_registry_schema(store)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM artifact_registry_schema_migrations"
        ).fetchone()
        assert row is not None
        assert row["version"] == ARTIFACT_REGISTRY_SCHEMA_VERSION


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


def test_schema_fails_closed_when_owned_table_is_missing(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_verifications")

    with pytest.raises(RuntimeError, match="schema mismatch"):
        initialize_artifact_registry_schema(store)


def test_schema_fails_closed_when_owned_table_is_malformed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_verifications")
        conn.execute("DROP TABLE artifact_registry_records")
        conn.execute("CREATE TABLE artifact_registry_records(artifact_id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="schema mismatch"):
        initialize_artifact_registry_schema(store)


def test_schema_rejects_non_integer_migration_storage(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_schema_migrations")
        conn.execute(
            "CREATE TABLE artifact_registry_schema_migrations ("
            "version REAL PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO artifact_registry_schema_migrations(version, applied_at) VALUES (?, ?)",
            (1.5, "tampered"),
        )

    with pytest.raises(RuntimeError, match="schema mismatch"):
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
    common = {
        "artifact_id": "a" * 64,
        "idempotency_key": "idempotent",
        "workspace_id": "workspace",
        "kind": "report",
        "location_kind": ArtifactLocationKind.OPAQUE_REFERENCE,
        "sha256": "b" * 64,
        "size_bytes": 1,
    }
    with pytest.raises(ValidationError, match="credential material"):
        ArtifactRecord(**common, locator="https://example.test/file?token=secret")
    with pytest.raises(ValidationError, match="secret material"):
        ArtifactRecord(**common, locator="blob:safe", metadata={"password": "secret"})
    with pytest.raises(ValidationError, match="credential material"):
        ArtifactRecord(**common, locator="blob:safe", metadata={"note": "Bearer top-secret"})


@pytest.mark.parametrize(
    "reference",
    (
        "https://example.test/object?api_key=canary",
        "https://example.test/object?api%5Fkey=canary",
        "https://example.test/object?client_secret=canary",
        "https://example.test/object?x-api-key=canary",
    ),
)
def test_common_credential_locators_are_rejected(reference: str) -> None:
    record = {
        "artifact_id": "a" * 64,
        "idempotency_key": "idempotent",
        "workspace_id": "workspace",
        "kind": "report",
        "location_kind": ArtifactLocationKind.OPAQUE_REFERENCE,
        "sha256": "b" * 64,
        "size_bytes": 1,
    }
    with pytest.raises(ValidationError, match="credential material"):
        ArtifactRecord(**record, locator=reference)


@pytest.mark.parametrize("key", ("client_secret", "x-api-key", "api-token"))
def test_common_credential_metadata_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="secret material"):
        ArtifactRecord(
            artifact_id="a" * 64,
            idempotency_key="idempotent",
            workspace_id="workspace",
            kind="report",
            location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
            locator="blob:safe",
            sha256="b" * 64,
            size_bytes=1,
            metadata={key: "canary"},
        )


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
        clock=lambda: datetime.fromisoformat("2026-08-26T20:00:00"),
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


def test_local_file_registration_is_disabled_without_explicit_root(tmp_path: Path) -> None:
    registry = ArtifactRegistry.from_store(SQLiteStore(tmp_path / "state.sqlite3"))
    source = tmp_path / "private.txt"
    source.write_text("private", encoding="utf-8")

    with pytest.raises(ArtifactRegistryError, match="allowed root"):
        registry.register_file(
            workspace_id="workspace-a",
            idempotency_key="private",
            path=source,
            kind="evidence",
        )


def test_local_file_registration_rejects_paths_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    source = denied / "outside.txt"
    source.write_text("outside", encoding="utf-8")
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "state.sqlite3"),
        local_file_roots=(allowed,),
    )

    with pytest.raises(ArtifactRegistryError, match="escapes configured"):
        registry.register_file(
            workspace_id="workspace-a",
            idempotency_key="outside",
            path=source,
            kind="evidence",
        )


def test_record_json_cannot_rebind_primary_key_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)
    record = registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="artifact-a",
        reference="blob:artifact-a",
        sha256="a" * 64,
        size_bytes=1,
        kind="evidence",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT record_json FROM artifact_registry_records WHERE artifact_id = ?",
            (record.artifact_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["record_json"])
        payload["artifact_id"] = "f" * 64
        conn.execute(
            "UPDATE artifact_registry_records SET record_json = ? WHERE artifact_id = ?",
            (json.dumps(payload), record.artifact_id),
        )

    with pytest.raises(ArtifactRegistryError, match="indexed metadata"):
        registry.get(record.artifact_id)


def test_index_columns_cannot_launder_workspace_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)
    registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="artifact-a",
        reference="blob:artifact-a",
        sha256="a" * 64,
        size_bytes=1,
        kind="evidence",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE artifact_registry_records SET workspace_id = ?",
            ("workspace-forged",),
        )

    with pytest.raises(ArtifactRegistryError, match="indexed metadata"):
        registry.list(workspace_id="workspace-forged")


def test_verification_json_cannot_rebind_artifact_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store)
    record = registry.register_reference(
        workspace_id="workspace-a",
        idempotency_key="artifact-a",
        reference="blob:artifact-a",
        sha256="a" * 64,
        size_bytes=1,
        kind="evidence",
    )
    verification = registry.verify(record.artifact_id)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT verification_json FROM artifact_registry_verifications "
            "WHERE verification_id = ?",
            (verification.verification_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["verification_json"])
        payload["artifact_id"] = "f" * 64
        conn.execute(
            "UPDATE artifact_registry_verifications SET verification_json = ? "
            "WHERE verification_id = ?",
            (json.dumps(payload), verification.verification_id),
        )

    with pytest.raises(ArtifactRegistryError, match="indexed metadata"):
        registry.verification_history(record.artifact_id)


def test_verify_rejects_post_registration_symlink_substitution(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = allowed / "artifact.bin"
    source.write_bytes(b"trusted-bytes")
    external = outside / "artifact.bin"
    external.write_bytes(b"trusted-bytes")

    store = SQLiteStore(tmp_path / "state.sqlite3")
    registry = ArtifactRegistry.from_store(store, local_file_roots=(allowed,))
    record = registry.register_file(
        workspace_id="workspace-a",
        idempotency_key="local-a",
        path=source,
        kind="evidence",
    )
    source.unlink()
    try:
        source.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(ArtifactRegistryError, match="link|substitution|escapes"):
        registry.verify(record.artifact_id)
