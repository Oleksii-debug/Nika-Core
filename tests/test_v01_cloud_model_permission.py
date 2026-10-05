from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.v01_cloud_model_permission import (
    CloudModelGrantRequest,
    CloudModelPermissionDenied,
    V01CloudModelPermissionService,
)
from nika_core.v01_model_settings import V01ModelSettings


NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "cloud-permission.sqlite3")
    store.initialize()
    return store


def _settings(
    store: SQLiteStore,
    *,
    route: str = "openai_compatible",
    private_data_allowed: bool = True,
) -> V01ModelSettings:
    settings = V01ModelSettings(store)
    if route == "openai_compatible":
        payload = {
            "schema_version": 1,
            "route_kind": route,
            "provider_id": "configured-api",
            "model": "api-model",
            "base_url": "https://api.example.test/v1",
            "credential_ref": "env:NIKA_TEST_SECRET_REF",
            "private_data_allowed": private_data_allowed,
            "timeout_seconds": 30,
            "revision": 0,
        }
    else:
        payload = {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    assert settings.configure(payload).status == "completed"
    return settings


def _task(store: SQLiteStore, settings: V01ModelSettings):
    queue = TaskQueue(store)
    payload = settings.prepare_task_payload({"command": "Run exact task."})
    return queue.create(workspace_id="default", agent_id="nika.default", payload=payload)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="fixture"),),
        model="api-model",
        provider_id="configured-api",
        provider_kind=ProviderKind.CLOUD,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=2.0,
    )


def _provider() -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="configured-api",
        kind=ProviderKind.CLOUD,
        supports_private_data=True,
        effect_network_host="api.example.test",
    )


def _authorize(service: V01CloudModelPermissionService, task_id: str) -> None:
    authority = service.execution_authority_for_task(task_id)
    assert authority is not None

    async def scenario() -> None:
        with service.cloud_effect_authorizer.execution_scope(authority):
            service.cloud_effect_authorizer.authorize_cloud_effect(
                request=_request(),
                provider=_provider(),
            )

    asyncio.run(scenario())


def test_local_task_never_prompts_or_receives_cloud_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store, route="ollama")
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    assert prompts == []
    assert service.execution_authority_for_task(record.task_id) is None


def test_cloud_task_without_private_data_permission_fails_before_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store, private_data_allowed=False)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="приватні дані"):
        service.admit_created_task(record)

    assert prompts == []
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 0


def test_binding_failure_rolls_back_new_permission_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )

    def reject_binding(**_kwargs: object) -> None:
        raise RuntimeError("simulated durable binding failure")

    monkeypatch.setattr(service, "_bind_permission", reject_binding)

    with pytest.raises(CloudModelPermissionDenied, match="зберегти дозвіл"):
        service.admit_created_task(record)

    with store.connection() as conn:
        permission_count = conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0]
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission'"
        ).fetchone()[0]

    assert permission_count == 0
    assert binding_count == 0
    assert audit_count == 0


def test_abrupt_exit_during_binding_rolls_back_permission_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )

    def abort_binding(**_kwargs: object) -> None:
        raise SystemExit("simulated process exit during binding")

    monkeypatch.setattr(service, "_bind_permission", abort_binding)

    with pytest.raises(SystemExit, match="simulated process exit"):
        service.admit_created_task(record)

    with store.connection() as conn:
        permission_count = conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0]
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission'"
        ).fetchone()[0]

    assert permission_count == 0
    assert binding_count == 0
    assert audit_count == 0


def test_task_change_during_confirmation_rolls_back_grant_and_binding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    queue = TaskQueue(store)

    def confirm(_request: CloudModelGrantRequest) -> bool:
        queue.transition(record.task_id, TaskState.READY)
        return True

    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=confirm,
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="зберегти дозвіл"):
        service.admit_created_task(record)

    assert queue.get(record.task_id).state is TaskState.READY
    with store.connection() as conn:
        permission_count = conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0]
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission'"
        ).fetchone()[0]

    assert permission_count == 0
    assert binding_count == 0
    assert audit_count == 0


def test_cloud_denial_creates_no_spendable_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: False,
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="не дозволено"):
        service.admit_created_task(record)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 0


@pytest.mark.parametrize("decision", [1, "yes", object()])
def test_non_boolean_host_confirmation_fails_closed(
    tmp_path: Path,
    decision: object,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: decision,  # type: ignore[return-value]
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="некоректний результат"):
        service.admit_created_task(record)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 0


def test_exact_secret_free_grant_admits_only_running_task_and_revocation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    service.admit_created_task(record)

    assert prompts == [
        CloudModelGrantRequest(
            task_id=record.task_id,
            provider_id="configured-api",
            model="api-model",
            network_host="api.example.test",
            private_data_allowed=True,
        )
    ]
    assert not hasattr(prompts[0], "credential_ref")
    assert "NIKA_TEST_SECRET_REF" not in repr(prompts[0])
    assert service.execution_authority_for_task(record.task_id) is None

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    assert service.execution_authority_for_task(record.task_id) is None
    queue.transition(record.task_id, TaskState.RUNNING)

    _authorize(service, record.task_id)

    service.revoke_task(record.task_id)
    assert service.execution_authority_for_task(record.task_id) is None


def test_durable_grant_reconstructs_after_restart_without_reprompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    first = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )
    first.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)

    restarted_store = SQLiteStore(store.path)
    restarted_settings = V01ModelSettings(restarted_store)
    restarted = V01CloudModelPermissionService(
        store=restarted_store,
        settings=restarted_settings,
        confirm=lambda _request: (_ for _ in ()).throw(
            AssertionError("durable authority reconstruction must not prompt again")
        ),
        clock=lambda: NOW,
    )

    _authorize(restarted, record.task_id)


def test_paused_task_cannot_spend_existing_grant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    assert service.execution_authority_for_task(record.task_id) is not None

    queue.transition(record.task_id, TaskState.PAUSED)
    assert service.execution_authority_for_task(record.task_id) is None


def test_resume_with_live_grant_does_not_reprompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    queue.transition(record.task_id, TaskState.PAUSED)
    service.admit_resumed_task(queue.get(record.task_id))

    assert len(prompts) == 1
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 1


def test_expired_grant_requires_new_resume_consent_and_new_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    instant = [NOW]
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: instant[0],
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    queue.transition(record.task_id, TaskState.PAUSED)

    instant[0] = NOW + timedelta(hours=25)
    assert service.execution_authority_for_task(record.task_id) is None
    service.admit_resumed_task(queue.get(record.task_id))

    assert len(prompts) == 2
    with store.connection() as conn:
        permissions = conn.execute(
            "SELECT permission_id, revoked_at FROM standing_permissions ORDER BY rowid"
        ).fetchall()
        binding = conn.execute(
            "SELECT permission_id FROM v01_cloud_model_permission_bindings "
            "WHERE task_id = ?",
            (record.task_id,),
        ).fetchone()
    assert len(permissions) == 2
    assert permissions[0]["permission_id"] != permissions[1]["permission_id"]
    assert binding["permission_id"] == permissions[1]["permission_id"]

    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    _authorize(service, record.task_id)


def test_revoked_grant_requires_new_resume_consent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    queue.transition(record.task_id, TaskState.PAUSED)
    service.revoke_task(record.task_id)

    assert service.execution_authority_for_task(record.task_id) is None
    service.admit_resumed_task(queue.get(record.task_id))

    assert len(prompts) == 2
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT revoked_at FROM standing_permissions ORDER BY rowid"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["revoked_at"] is None


def test_denied_resume_reconsent_leaves_task_paused_and_old_expired_binding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    instant = [NOW]
    decisions = [True, False]

    def confirm(_request: CloudModelGrantRequest) -> bool:
        return decisions.pop(0)

    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=confirm,
        clock=lambda: instant[0],
    )
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    queue.transition(record.task_id, TaskState.PAUSED)
    instant[0] = NOW + timedelta(hours=25)

    with pytest.raises(CloudModelPermissionDenied, match="не дозволено"):
        service.admit_resumed_task(queue.get(record.task_id))

    assert queue.get(record.task_id).state is TaskState.PAUSED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM standing_permissions").fetchone()[0] == 1


def test_binding_schema_rejects_text_version_storage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_schema ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
            "VALUES (?, ?)",
            ("1", NOW.isoformat()),
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="schema shape is invalid"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


def test_binding_schema_requires_canonical_foreign_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_schema ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
            "VALUES (?, ?)",
            (1, NOW.isoformat()),
        )
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_bindings ("
            "task_id TEXT PRIMARY KEY, permission_id TEXT NOT NULL UNIQUE, "
            "updated_at TEXT NOT NULL)"
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="foreign keys are invalid"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


def test_binding_schema_requires_unique_permission_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_schema ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
            "VALUES (?, ?)",
            (1, NOW.isoformat()),
        )
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_bindings ("
            "task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE, "
            "permission_id TEXT NOT NULL "
            "REFERENCES standing_permissions(permission_id), "
            "updated_at TEXT NOT NULL)"
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="permission_id must be unique"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )
