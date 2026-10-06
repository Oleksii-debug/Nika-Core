from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
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
from nika_core.security.standing_permission import PermissionContext, StandingPermissionScope
from nika_core.v01_cloud_model_permission import (
    CloudModelGrantRequest,
    CloudModelPermissionDenied,
    V01CloudModelPermissionService,
)
from nika_core.tools import ToolRisk
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


def test_binding_failure_rolls_back_newly_minted_permission(
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

    revoke_calls: list[str] = []

    def reject_compensating_revoke(permission_id: str, **_kwargs: object) -> None:
        revoke_calls.append(permission_id)
        raise AssertionError("atomic grant failure must not use compensating revoke")

    monkeypatch.setattr(service, "_bind_permission", reject_binding)
    monkeypatch.setattr(service._permissions, "revoke", reject_compensating_revoke)

    with pytest.raises(CloudModelPermissionDenied, match="зберегти дозвіл"):
        service.admit_created_task(record)

    with store.connection() as conn:
        permissions = conn.execute(
            "SELECT permission_id, revoked_at FROM standing_permissions"
        ).fetchall()
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission'"
        ).fetchone()[0]

    assert permissions == []
    assert binding_count == 0
    assert audit_count == 0
    assert revoke_calls == []


def test_abrupt_exit_during_binding_rolls_back_permission_binding_and_audit(
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
        raise SystemExit("simulated process exit during cloud binding")

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


def test_task_change_during_confirmation_rolls_back_grant_and_does_not_bind(
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
        permissions = conn.execute(
            "SELECT permission_id, revoked_at FROM standing_permissions"
        ).fetchall()
        binding_count = conn.execute(
            "SELECT COUNT(*) FROM v01_cloud_model_permission_bindings"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission'"
        ).fetchone()[0]

    assert permissions == []
    assert binding_count == 0
    assert audit_count == 0


def test_reentrant_confirmation_cannot_replace_binding_after_consent_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    service_holder: list[V01CloudModelPermissionService] = []
    nested = [False]

    def confirm(request: CloudModelGrantRequest) -> bool:
        prompts.append(request)
        if not nested[0]:
            nested[0] = True
            service_holder[0].admit_created_task(record)
        return True

    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=confirm,
        clock=lambda: NOW,
    )
    service_holder.append(service)

    with pytest.raises(CloudModelPermissionDenied, match="зберегти дозвіл"):
        service.admit_created_task(record)

    assert [prompt.task_id for prompt in prompts] == [record.task_id, record.task_id]
    with store.connection() as conn:
        permissions = conn.execute(
            "SELECT permission_id FROM standing_permissions ORDER BY rowid"
        ).fetchall()
        bindings = conn.execute(
            "SELECT task_id, permission_id FROM v01_cloud_model_permission_bindings"
        ).fetchall()
        grant_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = 'standing_permission' "
            "AND event_type = 'standing_permission.granted'"
        ).fetchone()[0]

    assert len(permissions) == 1
    assert len(bindings) == 1
    assert bindings[0]["task_id"] == record.task_id
    assert bindings[0]["permission_id"] == permissions[0]["permission_id"]
    assert grant_audits == 1


def test_confirmation_cannot_retarget_grant_to_caller_mutated_task_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    source = _task(store, settings)
    victim = _task(store, settings)
    source_id = source.task_id
    victim_id = victim.task_id
    prompts: list[CloudModelGrantRequest] = []

    def confirm(request: CloudModelGrantRequest) -> bool:
        prompts.append(request)
        object.__setattr__(source, "task_id", victim.task_id)
        object.__setattr__(source, "workspace_id", victim.workspace_id)
        object.__setattr__(source, "agent_id", victim.agent_id)
        object.__setattr__(source, "state", victim.state)
        object.__setattr__(source, "payload", victim.payload)
        return True

    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=confirm,
        clock=lambda: NOW,
    )

    service.admit_created_task(source)

    assert [prompt.task_id for prompt in prompts] == [source_id]
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT task_id FROM v01_cloud_model_permission_bindings ORDER BY task_id"
        ).fetchall()
    assert [row["task_id"] for row in rows] == [source_id]
    assert victim_id not in {row["task_id"] for row in rows}

    queue = TaskQueue(store)
    queue.transition(source_id, TaskState.READY)
    queue.transition(source_id, TaskState.RUNNING)
    _authorize(service, source_id)


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("provider_id", "forged-provider"),
        ("model", "forged-model"),
        ("network_host", "forged.example.test"),
    ),
)
def test_confirmation_callback_cannot_mutate_granted_authority(
    tmp_path: Path,
    field: str,
    mutated: str,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []

    def confirm(request: CloudModelGrantRequest) -> bool:
        prompts.append(request)
        object.__setattr__(request, field, mutated)
        return True

    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=confirm,
        clock=lambda: NOW,
    )

    service.admit_created_task(record)

    assert getattr(prompts[0], field) == mutated
    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    _authorize(service, record.task_id)


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


def test_revoked_preflight_cannot_hide_new_active_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    first_id = service._bound_permission_id(record.task_id, strict=True)
    assert first_id is not None
    service._permissions.revoke(first_id, revoked_at=NOW)

    second_id = service._new_permission_id(record.task_id)
    service._permissions.grant(
        permission_id=second_id,
        scope=StandingPermissionScope(
            subject_id="nika.packaged.model",
            context=service._context(record),
            action_class="model.cloud.complete",
            targets=(prompts[0].provider_id,),
            sites=(prompts[0].network_host,),
            resources=(prompts[0].model,),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        ),
    )

    original_bound = service._bound_permission_id
    injected = [False]

    def raced_bound(
        task_id: str,
        *,
        strict: bool,
        connection=None,
    ):
        if not injected[0] and connection is None:
            injected[0] = True
            with store.connection() as conn:
                conn.execute(
                    "UPDATE v01_cloud_model_permission_bindings "
                    "SET permission_id = ?, updated_at = ? WHERE task_id = ?",
                    (second_id, NOW.isoformat(), record.task_id),
                )
            return first_id
        return original_bound(
            task_id,
            strict=strict,
            connection=connection,
        )

    monkeypatch.setattr(service, "_bound_permission_id", raced_bound)

    with pytest.raises(CloudModelPermissionDenied, match="змінився під час відкликання"):
        service.revoke_task(record.task_id)

    second = service._permissions.get(second_id)
    assert second is not None and second.revoked_at is None
    assert original_bound(record.task_id, strict=True) == second_id


def test_revoke_race_rolls_back_stale_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert len(prompts) == 1

    first_id = service._bound_permission_id(record.task_id, strict=True)
    assert first_id is not None
    second_id = service._new_permission_id(record.task_id)
    service._permissions.grant(
        permission_id=second_id,
        scope=StandingPermissionScope(
            subject_id="nika.packaged.model",
            context=service._context(record),
            action_class="model.cloud.complete",
            targets=(prompts[0].provider_id,),
            sites=(prompts[0].network_host,),
            resources=(prompts[0].model,),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        ),
    )

    original = service._permissions.revoke_transaction

    @contextmanager
    def raced_revoke(permission_id: str, *, revoked_at: datetime | None = None):
        with store.connection() as conn:
            conn.execute(
                "UPDATE v01_cloud_model_permission_bindings "
                "SET permission_id = ?, updated_at = ? WHERE task_id = ?",
                (second_id, NOW.isoformat(), record.task_id),
            )
        with original(permission_id, revoked_at=revoked_at) as transaction:
            yield transaction

    monkeypatch.setattr(service._permissions, "revoke_transaction", raced_revoke)

    with pytest.raises(CloudModelPermissionDenied, match="змінився під час відкликання"):
        service.revoke_task(record.task_id)

    first = service._permissions.get(first_id)
    second = service._permissions.get(second_id)
    assert first is not None and first.revoked_at is None
    assert second is not None and second.revoked_at is None
    assert service._bound_permission_id(record.task_id, strict=True) == second_id


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



def test_binding_schema_rejects_partial_permission_unique_index(tmp_path: Path) -> None:
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
        conn.execute(
            "CREATE UNIQUE INDEX partial_cloud_permission_identity "
            "ON v01_cloud_model_permission_bindings(permission_id) "
            "WHERE task_id = 'only-this-task'"
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="permission_id must be unique"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


def test_binding_schema_rejects_preexisting_table_without_migration(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_bindings ("
            "task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE, "
            "permission_id TEXT NOT NULL UNIQUE "
            "REFERENCES standing_permissions(permission_id), "
            "updated_at TEXT NOT NULL)"
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="exists without schema version"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


def test_binding_schema_rejects_extra_migration_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
            "VALUES (?, ?)",
            (0, NOW.isoformat()),
        )

    with pytest.raises(RuntimeError, match="migration history is invalid"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


def test_binding_schema_rejects_noncanonical_migration_timestamp(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_schema ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_schema(version, applied_at) "
            "VALUES (?, ?)",
            (1, "2026-10-05T06:30:00+02:00"),
        )
        conn.execute(
            "CREATE TABLE v01_cloud_model_permission_bindings ("
            "task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE, "
            "permission_id TEXT NOT NULL UNIQUE "
            "REFERENCES standing_permissions(permission_id), "
            "updated_at TEXT NOT NULL)"
        )

    settings = _settings(store)
    with pytest.raises(RuntimeError, match="migration timestamp is invalid"):
        V01CloudModelPermissionService(
            store=store,
            settings=settings,
            confirm=lambda _request: True,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    "corrupt_updated_at",
    (
        "2026-10-05T04:30:00",
        "2026-10-05T06:30:00+02:00",
    ),
)
def test_corrupt_binding_timestamp_fails_before_resume_prompt(
    tmp_path: Path,
    corrupt_updated_at: str,
) -> None:
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
    queue.transition(record.task_id, TaskState.PAUSED)
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_cloud_model_permission_bindings SET updated_at = ? "
            "WHERE task_id = ?",
            (corrupt_updated_at, record.task_id),
        )

    prompts: list[CloudModelGrantRequest] = []
    restarted = V01CloudModelPermissionService(
        store=SQLiteStore(store.path),
        settings=V01ModelSettings(SQLiteStore(store.path)),
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )

    with pytest.raises(CloudModelPermissionDenied, match="пошкоджено"):
        restarted.admit_resumed_task(queue.get(record.task_id))

    assert prompts == []
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 1


def test_corrupt_binding_timestamp_storage_type_blocks_execution(
    tmp_path: Path,
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
    service.admit_created_task(record)

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_cloud_model_permission_bindings SET updated_at = ? "
            "WHERE task_id = ?",
            (sqlite3.Binary(NOW.isoformat().encode("utf-8")), record.task_id),
        )

    assert service.execution_authority_for_task(record.task_id) is None


class _DateTimeSubclass(datetime):
    pass


def test_cloud_permission_clock_rejects_datetime_subclass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    forged = _DateTimeSubclass(2026, 10, 5, 4, 30, tzinfo=UTC)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: forged,
    )

    with pytest.raises(RuntimeError, match="exact timezone-aware datetime"):
        service.admit_created_task(record)

    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 0


def test_recovered_running_task_with_live_grant_does_not_reprompt(
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

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    service.admit_recovered_task(queue.get(record.task_id))

    assert len(prompts) == 1
    assert service.execution_authority_for_task(record.task_id) is not None


def test_recovered_running_task_with_expired_grant_requires_new_consent(
    tmp_path: Path,
) -> None:
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
    first_id = service._bound_permission_id(record.task_id, strict=True)
    instant[0] = NOW + timedelta(hours=25)

    service.admit_recovered_task(queue.get(record.task_id))

    assert len(prompts) == 2
    second_id = service._bound_permission_id(record.task_id, strict=True)
    assert first_id is not None
    assert second_id is not None
    assert second_id != first_id
    assert service.execution_authority_for_task(record.task_id) is not None


def test_recovered_legacy_task_without_frozen_model_selection_never_prompts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    prompts: list[CloudModelGrantRequest] = []
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda request: prompts.append(request) or True,
        clock=lambda: NOW,
    )
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "legacy deterministic crash-left work"},
    )
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)

    service.admit_recovered_task(queue.get(record.task_id))

    assert prompts == []
    assert service.execution_authority_for_task(record.task_id) is None


def test_recovered_running_task_with_corrupt_bound_selection_fails_before_reprompt(
    tmp_path: Path,
) -> None:
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
    with store.connection() as conn:
        row = conn.execute(
            "SELECT selection_json FROM v01_task_model_bindings WHERE task_id = ?",
            (record.task_id,),
        ).fetchone()
        assert row is not None
        body = row["selection_json"]
        assert '"model":"api-model"' in body
        corrupt = body.replace(
            '"model":"api-model"',
            '"model":"shadow-model","model":"api-model"',
            1,
        )
        conn.execute(
            "UPDATE v01_task_model_bindings SET selection_json = ? WHERE task_id = ?",
            (corrupt, record.task_id),
        )

    instant[0] = NOW + timedelta(hours=25)
    with pytest.raises(CloudModelPermissionDenied, match="збережений маршрут"):
        service.admit_recovered_task(queue.get(record.task_id))

    assert len(prompts) == 1
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "mismatch",
    ("context", "action", "provider", "host", "model"),
)
def test_recovered_running_task_reconsents_when_live_grant_scope_mismatches(
    tmp_path: Path,
    mismatch: str,
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

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    authority = service.execution_authority_for_task(record.task_id)
    assert authority is not None
    selection = settings.for_task(record.task_id)
    request = service._grant_request(record, selection)

    context = authority.context
    action_class = "model.cloud.complete"
    provider_id = request.provider_id
    network_host = request.network_host
    model = request.model
    if mismatch == "context":
        context = PermissionContext(
            user_id=context.user_id,
            project_id=context.project_id,
            task_id=f"{context.task_id}-other",
        )
    elif mismatch == "action":
        action_class = "model.cloud.other"
    elif mismatch == "provider":
        provider_id = "other-provider"
    elif mismatch == "host":
        network_host = "other.example.test"
    elif mismatch == "model":
        model = "other-model"
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(f"unexpected mismatch: {mismatch}")

    wrong_permission_id = service._new_permission_id(record.task_id)
    service._permissions.grant(
        permission_id=wrong_permission_id,
        scope=StandingPermissionScope(
            subject_id=authority.subject_id,
            context=context,
            action_class=action_class,
            targets=(provider_id,),
            sites=(network_host,),
            resources=(model,),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        ),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_cloud_model_permission_bindings "
            "SET permission_id = ?, updated_at = ? WHERE task_id = ?",
            (wrong_permission_id, NOW.isoformat(), record.task_id),
        )

    assert service.execution_authority_for_task(record.task_id) is None

    service.admit_recovered_task(queue.get(record.task_id))

    assert len(prompts) == 2
    assert prompts[1] == prompts[0]
    assert service._bound_permission_id(record.task_id, strict=True) != wrong_permission_id
    assert service.execution_authority_for_task(record.task_id) is not None
    _authorize(service, record.task_id)


def test_recovered_reconsent_rolls_back_if_model_binding_changes_during_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    instant = [NOW]

    def confirm(request: CloudModelGrantRequest) -> bool:
        prompts.append(request)
        if len(prompts) == 2:
            with store.connection() as conn:
                row = conn.execute(
                    "SELECT selection_json FROM v01_task_model_bindings WHERE task_id = ?",
                    (record.task_id,),
                ).fetchone()
                assert row is not None
                body = row["selection_json"]
                assert '"model":"api-model"' in body
                conn.execute(
                    "UPDATE v01_task_model_bindings SET selection_json = ? "
                    "WHERE task_id = ?",
                    (
                        body.replace(
                            '"model":"api-model"',
                            '"model":"shadow-model","model":"api-model"',
                            1,
                        ),
                        record.task_id,
                    ),
                )
        return True

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
    first_id = service._bound_permission_id(record.task_id, strict=True)
    assert first_id is not None
    instant[0] = NOW + timedelta(hours=25)

    with pytest.raises(CloudModelPermissionDenied, match="зберегти дозвіл"):
        service.admit_recovered_task(queue.get(record.task_id))

    assert len(prompts) == 2
    assert service._bound_permission_id(record.task_id, strict=True) == first_id
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions"
        ).fetchone()[0] == 1


def test_injected_live_grant_cannot_bypass_private_data_setting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store, private_data_allowed=False)
    record = _task(store, settings)
    service = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
        clock=lambda: NOW,
    )

    selection = settings.for_task(record.task_id)
    request = service._grant_request(record, selection)
    permission_id = service._new_permission_id(record.task_id)
    service._permissions.grant(
        permission_id=permission_id,
        scope=service._scope_for_request(
            record,
            request,
            granted_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        ),
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_cloud_model_permission_bindings"
            "(task_id, permission_id, updated_at) VALUES (?, ?, ?)",
            (record.task_id, permission_id, NOW.isoformat()),
        )

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)

    assert service.execution_authority_for_task(record.task_id) is None


def test_recovered_reconsent_dates_new_grant_after_confirmation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = _settings(store)
    record = _task(store, settings)
    prompts: list[CloudModelGrantRequest] = []
    instant = [NOW]

    def confirm(request: CloudModelGrantRequest) -> bool:
        prompts.append(request)
        if len(prompts) == 2:
            instant[0] += timedelta(hours=25)
        return True

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
    instant[0] = NOW + timedelta(hours=25)

    service.admit_recovered_task(queue.get(record.task_id))

    assert instant[0] == NOW + timedelta(hours=50)
    assert len(prompts) == 2
    permission_id = service._bound_permission_id(record.task_id, strict=True)
    assert permission_id is not None
    permission = service._permissions.get(permission_id)
    assert permission is not None
    assert permission.granted_at == instant[0]
    assert permission.expires_at == instant[0] + timedelta(hours=24)
    assert service.execution_authority_for_task(record.task_id) is not None


def test_corrupt_standing_permission_fails_resume_without_reprompt(
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

    queue = TaskQueue(store)
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    queue.transition(record.task_id, TaskState.PAUSED)
    permission_id = service._bound_permission_id(record.task_id, strict=True)
    assert permission_id is not None
    with store.connection() as conn:
        conn.execute(
            "UPDATE standing_permissions SET scope_fingerprint = ? "
            "WHERE permission_id = ?",
            ("0" * 64, permission_id),
        )

    with pytest.raises(CloudModelPermissionDenied, match="дозвіл.*пошкоджено"):
        service.admit_resumed_task(queue.get(record.task_id))

    assert len(prompts) == 1
    assert queue.get(record.task_id).state is TaskState.PAUSED
    assert service.execution_authority_for_task(record.task_id) is None
