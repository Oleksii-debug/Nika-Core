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
    ArtifactVerification,
    ArtifactVerificationState,
    SQLiteArtifactRepository,
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


def test_schema_rejects_missing_required_index(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP INDEX idx_artifact_registry_sha256")

    with pytest.raises(RuntimeError, match="index schema mismatch"):
        initialize_artifact_registry_schema(store)


def test_schema_rejects_missing_idempotency_unique_constraint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_verifications")
        conn.execute(
            "ALTER TABLE artifact_registry_records RENAME TO artifact_registry_records_old"
        )
        conn.execute(
            """CREATE TABLE artifact_registry_records (
                artifact_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                location_kind TEXT NOT NULL CHECK(location_kind IN ('local_file','opaque_reference')),
                producer_id TEXT,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute("DROP TABLE artifact_registry_records_old")
        conn.execute(
            "CREATE INDEX idx_artifact_registry_workspace_kind "
            "ON artifact_registry_records(workspace_id, kind, created_at, artifact_id)"
        )
        conn.execute(
            "CREATE INDEX idx_artifact_registry_sha256 "
            "ON artifact_registry_records(sha256, artifact_id)"
        )
        conn.execute(
            "CREATE INDEX idx_artifact_registry_workspace_producer "
            "ON artifact_registry_records(workspace_id, producer_id, created_at, artifact_id)"
        )
        conn.execute(
            """CREATE TABLE artifact_registry_verifications (
                verification_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('verified','missing','mismatch','unavailable')),
                verification_json TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(artifact_id) REFERENCES artifact_registry_records(artifact_id)
            )"""
        )
        conn.execute(
            "CREATE INDEX idx_artifact_registry_verifications "
            "ON artifact_registry_verifications(artifact_id, checked_at, verification_id)"
        )

    with pytest.raises(RuntimeError, match="unique constraint schema mismatch"):
        initialize_artifact_registry_schema(store)


def test_schema_rejects_missing_verification_foreign_key(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    with store.connection() as conn:
        conn.execute("DROP TABLE artifact_registry_verifications")
        conn.execute(
            """CREATE TABLE artifact_registry_verifications (
                verification_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('verified','missing','mismatch','unavailable')),
                verification_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX idx_artifact_registry_verifications "
            "ON artifact_registry_verifications(artifact_id, checked_at, verification_id)"
        )

    with pytest.raises(RuntimeError, match="foreign key schema mismatch"):
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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_id", "workspace-a\x00shared"),
        ("idempotency_key", "shared\x00effect"),
    ),
)
def test_artifact_identity_fields_reject_nul_delimiter_collisions(
    field: str,
    value: str,
) -> None:
    payload = {
        "artifact_id": "a" * 64,
        "idempotency_key": "effect",
        "workspace_id": "workspace-a",
        "kind": "result",
        "location_kind": ArtifactLocationKind.OPAQUE_REFERENCE,
        "locator": "blob:safe",
        "sha256": "b" * 64,
        "size_bytes": 1,
    }
    payload[field] = value

    with pytest.raises(ValidationError, match="must not contain NUL"):
        ArtifactRecord(**payload)


@pytest.mark.parametrize("value", (True, 1.0, "1"))
def test_artifact_size_fields_require_exact_integer_carriers(value: object) -> None:
    record = {
        "artifact_id": "a" * 64,
        "idempotency_key": "effect",
        "workspace_id": "workspace-a",
        "kind": "result",
        "location_kind": ArtifactLocationKind.OPAQUE_REFERENCE,
        "locator": "blob:safe",
        "sha256": "b" * 64,
    }
    with pytest.raises(ValidationError, match="valid integer"):
        ArtifactRecord(**record, size_bytes=value)

    verification = {
        "verification_id": "c" * 64,
        "artifact_id": "a" * 64,
        "state": ArtifactVerificationState.VERIFIED,
        "expected_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError, match="valid integer"):
        ArtifactVerification(
            **verification,
            expected_size_bytes=value,
            actual_size_bytes=value,
        )


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
        "https://example.test/callback#access_token=canary",
        "blob:refresh_token=canary",
        "https://example.test/private_key=canary",
        "https://example.test/object/secret_key=canary",
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


@pytest.mark.parametrize(
    "key",
    (
        "access-token",
        "client_secret",
        "private-key",
        "refresh_token",
        "secret-key",
        "x-api-key",
        "api-token",
    ),
)
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


def test_metadata_entry_count_is_bounded_before_durable_serialization() -> None:
    with pytest.raises(ValidationError, match="at most 256 entries"):
        ArtifactRecord(
            artifact_id="a" * 64,
            idempotency_key="idempotent",
            workspace_id="workspace",
            kind="report",
            location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
            locator="blob:safe",
            sha256="b" * 64,
            size_bytes=1,
            metadata={f"key-{index}": "value" for index in range(257)},
        )


def test_oversized_record_is_rejected_before_persistence(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    metadata = {f"key-{index:03d}": "x" * 4096 for index in range(256)}

    with pytest.raises(ArtifactRegistryError, match="1 MiB durable JSON limit"):
        registry.register_reference(
            workspace_id="workspace-a",
            idempotency_key="oversized-record",
            reference="blob:oversized",
            sha256="b" * 64,
            size_bytes=1,
            kind="report",
            metadata=metadata,
        )

    assert registry.list(workspace_id="workspace-a") == ()


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


def test_record_json_rejects_oversized_payload_before_rehydration(tmp_path: Path) -> None:
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
    oversized = '{"workspace_id":"' + ("x" * 1_048_577) + '"}'
    with store.connection() as conn:
        conn.execute(
            "UPDATE artifact_registry_records SET record_json = ? WHERE artifact_id = ?",
            (oversized, record.artifact_id),
        )

    with pytest.raises(ArtifactRegistryError, match="1 MiB durable JSON limit"):
        registry.get(record.artifact_id)


def test_record_json_rejects_duplicate_keys_even_when_last_value_matches_index(
    tmp_path: Path,
) -> None:
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
        raw = row["record_json"]
        assert type(raw) is str
        needle = '"workspace_id":"workspace-a"'
        assert raw.count(needle) == 1
        ambiguous = raw.replace(
            needle,
            '"workspace_id":"workspace-forged","workspace_id":"workspace-a"',
            1,
        )
        conn.execute(
            "UPDATE artifact_registry_records SET record_json = ? WHERE artifact_id = ?",
            (ambiguous, record.artifact_id),
        )

    with pytest.raises(ArtifactRegistryError, match="record payload is invalid"):
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


def test_verification_json_rejects_duplicate_keys_even_when_last_value_matches_index(
    tmp_path: Path,
) -> None:
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
        raw = row["verification_json"]
        assert type(raw) is str
        needle = '"state":"unavailable"'
        assert raw.count(needle) == 1
        ambiguous = raw.replace(
            needle,
            '"state":"verified","state":"unavailable"',
            1,
        )
        conn.execute(
            "UPDATE artifact_registry_verifications SET verification_json = ? "
            "WHERE verification_id = ?",
            (ambiguous, verification.verification_id),
        )

    with pytest.raises(ArtifactRegistryError, match="verification payload is invalid"):
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


@pytest.mark.parametrize("field", ("workspace_id", "idempotency_key"))
def test_registry_rejects_invalid_utf8_identity_before_persistence(
    tmp_path: Path,
    field: str,
) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    arguments = {
        "workspace_id": "workspace-a",
        "idempotency_key": "artifact-a",
        "reference": "blob:safe",
        "sha256": "a" * 64,
        "size_bytes": 1,
        "kind": "evidence",
    }
    arguments[field] = "\ud800"

    with pytest.raises(ArtifactRegistryError, match="UTF-8"):
        registry.register_reference(**arguments)

    assert registry.list() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("locator", "blob:\ud800"),
        ("display_name", "report-\ud800"),
        ("producer_id", "agent-\ud800"),
        ("metadata", {"note": "\ud800"}),
        ("metadata", {"key-\ud800": "value"}),
    ),
)
def test_artifact_record_rejects_non_utf8_durable_text(
    field: str,
    value: object,
) -> None:
    arguments = {
        "artifact_id": "a" * 64,
        "idempotency_key": "idempotent",
        "workspace_id": "workspace",
        "kind": "report",
        "location_kind": ArtifactLocationKind.OPAQUE_REFERENCE,
        "locator": "blob:safe",
        "sha256": "b" * 64,
        "size_bytes": 1,
    }
    arguments[field] = value

    with pytest.raises(ValidationError, match="UTF-8"):
        ArtifactRecord(**arguments)


@pytest.mark.parametrize(
    "reference",
    (
        "https://example.test/object?api_key%253Dcanary",
        "https://example.test/object?client%255Fsecret%253Dcanary",
        "blob:%2542earer%2520top-secret",
    ),
)
def test_layered_percent_encoded_credential_locators_are_rejected(reference: str) -> None:
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
        ArtifactRecord(**common, locator=reference)


@pytest.mark.parametrize(
    "metadata",
    (
        {"api%255Fkey": "canary"},
        {"note": "%2542earer%2520top-secret"},
    ),
)
def test_layered_percent_encoded_metadata_credentials_are_rejected(
    metadata: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="secret material|credential material"):
        ArtifactRecord(
            artifact_id="a" * 64,
            idempotency_key="idempotent",
            workspace_id="workspace",
            kind="report",
            location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
            locator="blob:safe",
            sha256="b" * 64,
            size_bytes=1,
            metadata=metadata,
        )


def test_record_rehydration_rejects_rebound_deterministic_identity(tmp_path: Path) -> None:
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
        payload["workspace_id"] = "workspace-rebound"
        conn.execute(
            "UPDATE artifact_registry_records SET workspace_id = ?, record_json = ? "
            "WHERE artifact_id = ?",
            ("workspace-rebound", json.dumps(payload), record.artifact_id),
        )

    with pytest.raises(ArtifactRegistryError, match="deterministic identity"):
        registry.get(record.artifact_id)


def test_verification_rehydration_rejects_actual_evidence_identity_drift(
    tmp_path: Path,
) -> None:
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
        payload["actual_sha256"] = "f" * 64
        payload["actual_size_bytes"] = 7
        conn.execute(
            "UPDATE artifact_registry_verifications SET verification_json = ? "
            "WHERE verification_id = ?",
            (json.dumps(payload), verification.verification_id),
        )

    with pytest.raises(ArtifactRegistryError, match="deterministic identity"):
        registry.verification_history(record.artifact_id)


def test_verification_rehydration_rejects_expected_metadata_drift(tmp_path: Path) -> None:
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
        payload["expected_sha256"] = "f" * 64
        payload["expected_size_bytes"] = 99
        conn.execute(
            "UPDATE artifact_registry_verifications SET verification_json = ? "
            "WHERE verification_id = ?",
            (json.dumps(payload), verification.verification_id),
        )

    with pytest.raises(ArtifactRegistryError, match="expected metadata"):
        registry.verification_history(record.artifact_id)


@pytest.mark.parametrize(
    "operation",
    (
        lambda registry: registry.get("\ud800"),
        lambda registry: registry.list(workspace_id="\ud800"),
        lambda registry: registry.list(kind="\ud800"),
        lambda registry: registry.list(producer_id="\ud800"),
        lambda registry: registry.find_by_sha256("\ud800"),
        lambda registry: registry.find_by_sha256("a" * 64, workspace_id="\ud800"),
        lambda registry: registry.verification_history("\ud800"),
    ),
)
def test_read_queries_reject_invalid_utf8_before_sql(
    tmp_path: Path,
    operation: object,
) -> None:
    registry = _registry(tmp_path / "state.sqlite3")

    with pytest.raises(ValueError, match="UTF-8"):
        operation(registry)


@pytest.mark.parametrize("field", ("limit", "offset"))
@pytest.mark.parametrize("value", (True, False, 1.0, "1"))
def test_list_pagination_requires_exact_integer_carriers(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    arguments = {field: value}

    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        registry.list(**arguments)


def test_read_queries_reject_oversized_text_before_sql(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "state.sqlite3")
    oversized = "я" * 2049

    with pytest.raises(ValueError, match="4096-byte query limit"):
        registry.list(workspace_id=oversized)


@pytest.mark.parametrize(
    ("state", "actual_sha256", "actual_size_bytes"),
    (
        (ArtifactVerificationState.VERIFIED, None, None),
        (ArtifactVerificationState.VERIFIED, "d" * 64, 4),
        (ArtifactVerificationState.MISSING, "c" * 64, 4),
        (ArtifactVerificationState.UNAVAILABLE, "c" * 64, 4),
        (ArtifactVerificationState.MISMATCH, "c" * 64, 4),
        (ArtifactVerificationState.MISMATCH, "c" * 64, None),
    ),
)
def test_verification_contract_rejects_false_or_partial_evidence(
    state: ArtifactVerificationState,
    actual_sha256: str | None,
    actual_size_bytes: int | None,
) -> None:
    with pytest.raises(ValidationError):
        ArtifactVerification(
            verification_id="a" * 64,
            artifact_id="b" * 64,
            state=state,
            expected_sha256="c" * 64,
            actual_sha256=actual_sha256,
            expected_size_bytes=4,
            actual_size_bytes=actual_size_bytes,
        )


def test_repository_rejects_forged_record_identity_before_write(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    initialize_artifact_registry_schema(store)
    repository = SQLiteArtifactRepository(store)
    record = ArtifactRecord(
        artifact_id="f" * 64,
        idempotency_key="artifact-a",
        workspace_id="workspace-a",
        kind="evidence",
        location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
        locator="blob:artifact-a",
        sha256="a" * 64,
        size_bytes=1,
    )

    with pytest.raises(ArtifactRegistryError, match="deterministic identity"):
        repository.put_record(record)

    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM artifact_registry_records").fetchone()[0]
    assert count == 0


def test_repository_revalidates_verification_model_copies_before_write(tmp_path: Path) -> None:
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
    repository = SQLiteArtifactRepository(store)
    forged = verification.model_copy(
        update={"state": ArtifactVerificationState.VERIFIED}
    )

    with pytest.raises(ArtifactRegistryError, match="verification input is invalid"):
        repository.put_verification(forged)

    assert registry.verification_history(record.artifact_id) == (verification,)


def test_repository_rejects_verification_expected_metadata_drift_before_write(
    tmp_path: Path,
) -> None:
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
    repository = SQLiteArtifactRepository(store)
    forged = verification.model_copy(update={"expected_sha256": "f" * 64})

    with pytest.raises(ArtifactRegistryError, match="expected metadata"):
        repository.put_verification(forged)

    assert registry.verification_history(record.artifact_id) == (verification,)

