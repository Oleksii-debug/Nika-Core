from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionConflictError,
    StandingPermissionPolicy,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec


def _context() -> PermissionContext:
    return PermissionContext(user_id="user-1", project_id="project-1", task_id="task-1")


def _scope(
    *,
    subject_id: str = "agent-parent",
    action_class: str = "browser.inspect",
    targets: tuple[str, ...] = ("target:listing-1",),
    sites: tuple[str, ...] = ("example.test",),
    resources: tuple[str, ...] = ("resource:price",),
    risk_ceiling: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT,
    context: PermissionContext | None = None,
    granted_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> StandingPermissionScope:
    start = granted_at or datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    return StandingPermissionScope(
        subject_id=subject_id,
        context=context or _context(),
        action_class=action_class,
        targets=targets,
        sites=sites,
        resources=resources,
        risk_ceiling=risk_ceiling,
        granted_at=start,
        expires_at=expires_at or start + timedelta(hours=2),
    )


def _use(
    *,
    subject_id: str = "agent-parent",
    context: PermissionContext | None = None,
    tool_id: str = "browser.inspect",
    target: str = "target:listing-1",
    site: str | None = "example.test",
    resource_id: str = "resource:price",
    risk: ToolRisk = ToolRisk.EXTERNAL_SIDE_EFFECT,
) -> StandingPermissionUse:
    return StandingPermissionUse(
        subject_id=subject_id,
        context=context or _context(),
        intent=ActionIntent(
            action_id="action-1",
            tool_id=tool_id,
            risk=risk,
            target=target,
            network_host=site,
        ),
        resource_id=resource_id,
    )


def _permissions(tmp_path, *, with_audit: bool = True):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store) if with_audit else None
    permissions = StandingPermissionStore(store, audit_log=audit)
    permissions.initialize()
    return store, audit, permissions


def _synchronize_initial_absence_reads(
    permissions: StandingPermissionStore,
    permission_id: str,
) -> None:
    both_observed_absent = Barrier(2)
    counter_lock = Lock()
    absent_reads = 0
    original_get = permissions._get

    def synchronized_get(conn, current_permission_id):
        nonlocal absent_reads
        record = original_get(conn, current_permission_id)
        should_wait = False
        if current_permission_id == permission_id and record is None:
            with counter_lock:
                if absent_reads < 2:
                    absent_reads += 1
                    should_wait = True
        if should_wait:
            both_observed_absent.wait(timeout=5)
        return record

    permissions._get = synchronized_get  # type: ignore[method-assign]


def test_exact_scope_authorizes_and_every_material_boundary_fails_closed(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(permission_id="perm-root", scope=_scope(granted_at=start))

    authorized = permissions.authorize("perm-root", _use(), now=start + timedelta(minutes=1))
    assert authorized.permission_id == "perm-root"

    mismatches = (
        _use(tool_id="browser.navigate"),
        _use(target="target:listing-2"),
        _use(site="other.test"),
        _use(resource_id="resource:title"),
        _use(subject_id="agent-child"),
        _use(context=PermissionContext(user_id="user-2", project_id="project-1", task_id="task-1")),
        _use(context=PermissionContext(user_id="user-1", project_id="project-2", task_id="task-1")),
        _use(context=PermissionContext(user_id="user-1", project_id="project-1", task_id="task-2")),
    )
    for mismatch in mismatches:
        with pytest.raises(PermissionError):
            permissions.authorize("perm-root", mismatch, now=start + timedelta(minutes=1))


def test_scope_is_exact_finite_and_cannot_be_rebound_under_same_authority(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    original = _scope()
    first = permissions.grant(permission_id="perm-root", scope=original)
    same = permissions.grant(permission_id="perm-root", scope=original)
    assert same.scope_fingerprint == first.scope_fingerprint

    changed_scopes = (
        replace(original, action_class="browser.navigate"),
        replace(original, targets=("target:listing-2",)),
        replace(original, sites=("other.test",)),
        replace(original, resources=("resource:title",)),
        replace(original, risk_ceiling=ToolRisk.LOCAL_WRITE),
        replace(original, context=PermissionContext("user-2", "project-1", "task-1")),
        replace(original, expires_at=original.expires_at - timedelta(minutes=1)),
    )
    for changed in changed_scopes:
        with pytest.raises(StandingPermissionConflictError, match="new authority"):
            permissions.grant(permission_id="perm-root", scope=changed)


def test_identical_concurrent_grants_converge_once_and_survive_restart(tmp_path) -> None:
    store, audit, permissions = _permissions(tmp_path)
    assert audit is not None
    scope = _scope()
    _synchronize_initial_absence_reads(permissions, "perm-concurrent")

    def grant():
        return permissions.grant(permission_id="perm-concurrent", scope=scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(grant), pool.submit(grant))
        first, second = (future.result(timeout=10) for future in futures)

    assert first.permission_id == second.permission_id == "perm-concurrent"
    assert first.scope_fingerprint == second.scope_fingerprint
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM standing_permissions WHERE permission_id = ?",
            ("perm-concurrent",),
        ).fetchone()["count"]
    assert count == 1
    assert tuple(
        event.event_type
        for event in audit.list_for(
            entity_type="standing_permission",
            entity_id="perm-concurrent",
        )
    ) == ("standing_permission.granted",)

    restarted = StandingPermissionStore(store, audit_log=AuditLog(store))
    restarted.initialize()
    replayed = restarted.grant(permission_id="perm-concurrent", scope=scope)
    assert replayed.scope_fingerprint == first.scope_fingerprint


def test_conflicting_concurrent_grants_keep_one_canonical_authority(tmp_path) -> None:
    store, audit, permissions = _permissions(tmp_path)
    assert audit is not None
    first_scope = _scope()
    second_scope = replace(first_scope, targets=("target:listing-2",))
    _synchronize_initial_absence_reads(permissions, "perm-conflict")

    def grant(scope):
        try:
            return permissions.grant(permission_id="perm-conflict", scope=scope)
        except StandingPermissionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(grant, first_scope), pool.submit(grant, second_scope))
        outcomes = tuple(future.result(timeout=10) for future in futures)

    records = tuple(
        outcome for outcome in outcomes if not isinstance(outcome, StandingPermissionConflictError)
    )
    conflicts = tuple(
        outcome for outcome in outcomes if isinstance(outcome, StandingPermissionConflictError)
    )
    assert len(records) == len(conflicts) == 1
    assert str(conflicts[0]) == (
        "permission id belongs to different authority; changed scope requires new authority"
    )
    canonical = permissions.get("perm-conflict")
    assert canonical is not None
    assert canonical.scope_fingerprint == records[0].scope_fingerprint
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM standing_permissions WHERE permission_id = ?",
            ("perm-conflict",),
        ).fetchone()["count"]
    assert count == 1
    assert tuple(
        event.event_type
        for event in audit.list_for(
            entity_type="standing_permission",
            entity_id="perm-conflict",
        )
    ) == ("standing_permission.granted",)


def test_identical_concurrent_delegations_converge_and_audit_once(tmp_path) -> None:
    store, audit, permissions = _permissions(tmp_path)
    assert audit is not None
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(
        permission_id="perm-parent",
        scope=_scope(
            targets=("target:listing-1", "target:listing-2"),
            resources=("resource:price", "resource:title"),
            granted_at=start,
            expires_at=start + timedelta(hours=4),
        ),
    )
    child = _scope(
        subject_id="agent-child",
        targets=("target:listing-1",),
        resources=("resource:price",),
        risk_ceiling=ToolRisk.READ_ONLY,
        granted_at=start + timedelta(minutes=1),
        expires_at=start + timedelta(hours=2),
    )
    _synchronize_initial_absence_reads(permissions, "perm-child-concurrent")

    def delegate():
        return permissions.delegate(
            parent_permission_id="perm-parent",
            permission_id="perm-child-concurrent",
            scope=child,
            delegated_by_subject_id="agent-parent",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(delegate), pool.submit(delegate))
        first, second = (future.result(timeout=10) for future in futures)

    assert first.permission_id == second.permission_id == "perm-child-concurrent"
    assert first.scope_fingerprint == second.scope_fingerprint
    assert tuple(
        event.event_type
        for event in audit.list_for(
            entity_type="standing_permission",
            entity_id="perm-child-concurrent",
        )
    ) == ("standing_permission.delegated",)

    restarted = StandingPermissionStore(store, audit_log=AuditLog(store))
    restarted.initialize()
    replayed = restarted.delegate(
        parent_permission_id="perm-parent",
        permission_id="perm-child-concurrent",
        scope=child,
        delegated_by_subject_id="agent-parent",
    )
    assert replayed.scope_fingerprint == first.scope_fingerprint


def test_approve_all_forever_shapes_and_high_impact_are_rejected() -> None:
    base = _scope()
    with pytest.raises(ValueError, match="broad|wildcard"):
        replace(base, action_class="all")
    with pytest.raises(ValueError, match="broad|wildcard"):
        replace(base, targets=("*",))
    with pytest.raises(ValueError, match="broad|wildcard"):
        replace(base, sites=("*.example.test",))
    with pytest.raises(ValueError, match="broad|wildcard"):
        replace(base, resources=("any",))
    with pytest.raises(ValueError, match="finite expiry"):
        replace(base, expires_at=base.granted_at)
    with pytest.raises(ValueError, match="high-impact"):
        replace(base, risk_ceiling=ToolRisk.HIGH_IMPACT)


def test_high_impact_use_never_inherits_standing_permission(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(permission_id="perm-root", scope=_scope(granted_at=start))
    with pytest.raises(PermissionError, match="fresh explicit"):
        permissions.authorize(
            "perm-root",
            _use(risk=ToolRisk.HIGH_IMPACT),
            now=start + timedelta(minutes=1),
        )


def test_expiry_is_enforced_at_exact_boundary(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    expiry = start + timedelta(minutes=10)
    permissions.grant(
        permission_id="perm-root",
        scope=_scope(granted_at=start, expires_at=expiry),
    )
    permissions.authorize("perm-root", _use(), now=expiry - timedelta(microseconds=1))
    with pytest.raises(PermissionError, match="expired"):
        permissions.authorize("perm-root", _use(), now=expiry)


def test_local_permission_has_explicit_empty_site_scope(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(
        permission_id="perm-local",
        scope=_scope(
            action_class="windows.inspect",
            targets=("window:calculator",),
            sites=(),
            resources=("control:display",),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=start,
        ),
    )
    permissions.authorize(
        "perm-local",
        _use(
            tool_id="windows.inspect",
            target="window:calculator",
            site=None,
            resource_id="control:display",
            risk=ToolRisk.READ_ONLY,
        ),
        now=start + timedelta(minutes=1),
    )
    with pytest.raises(PermissionError, match="site"):
        permissions.authorize(
            "perm-local",
            _use(
                tool_id="windows.inspect",
                target="window:calculator",
                site="example.test",
                resource_id="control:display",
                risk=ToolRisk.READ_ONLY,
            ),
            now=start + timedelta(minutes=1),
        )


def test_revoke_blocks_future_use_and_survives_restart(tmp_path) -> None:
    store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(permission_id="perm-root", scope=_scope(granted_at=start))
    permissions.authorize("perm-root", _use(), now=start + timedelta(minutes=1))
    permissions.revoke("perm-root", revoked_at=start + timedelta(minutes=2))

    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize("perm-root", _use(), now=start + timedelta(minutes=3))

    restarted = StandingPermissionStore(store, audit_log=AuditLog(store))
    restarted.initialize()
    record = restarted.get("perm-root")
    assert record is not None
    assert record.revoked_at == start + timedelta(minutes=2)
    with pytest.raises(PermissionError, match="revoked"):
        restarted.authorize("perm-root", _use(), now=start + timedelta(minutes=4))


def test_revoke_cannot_commit_inside_an_inflight_authorization_decision(tmp_path) -> None:
    class BlockingUseAudit:
        def __init__(self) -> None:
            self.use_checked = Event()
            self.release_use = Event()

        def append_with_connection(
            self,
            _conn,
            *,
            event_type: str,
            entity_type: str,
            entity_id: str,
            payload: dict[str, object],
        ) -> int:
            del entity_type, entity_id, payload
            if event_type == "standing_permission.used":
                self.use_checked.set()
                assert self.release_use.wait(timeout=5)
            return 1

        def append(self, **_kwargs) -> int:
            return 1

    store = SQLiteStore(tmp_path / "race.db")
    store.initialize()
    audit = BlockingUseAudit()
    permissions = StandingPermissionStore(store, audit_log=audit)  # type: ignore[arg-type]
    permissions.initialize()
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(permission_id="perm-race", scope=_scope(granted_at=start))
    revoke_started = Event()
    revoke_completed = Event()

    def authorize():
        return permissions.authorize(
            "perm-race",
            _use(),
            now=start + timedelta(minutes=1),
        )

    def revoke():
        revoke_started.set()
        try:
            return permissions.revoke(
                "perm-race",
                revoked_at=start + timedelta(minutes=2),
            )
        finally:
            revoke_completed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        authorized = pool.submit(authorize)
        assert audit.use_checked.wait(timeout=5)
        revoked = pool.submit(revoke)
        assert revoke_started.wait(timeout=5)
        assert not revoke_completed.wait(timeout=0.2)
        audit.release_use.set()
        assert authorized.result(timeout=5).permission_id == "perm-race"
        assert revoked.result(timeout=5).revoked_at == start + timedelta(minutes=2)

    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize(
            "perm-race",
            _use(),
            now=start + timedelta(minutes=3),
        )


def test_standing_policy_executes_and_restart_replays_only_current_exact_effect(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "integrated.db")
    store.initialize()
    task_id = TaskQueue(store).create(workspace_id="b06", agent_id="worker70").task_id
    context = PermissionContext(user_id="user-1", project_id="project-1", task_id=task_id)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
    permissions.initialize()
    permissions.grant(
        permission_id="perm-integrated",
        scope=_scope(context=context, granted_at=start),
    )
    binding = StandingPermissionBinding(
        permission_id="perm-integrated",
        subject_id="agent-parent",
        context=context,
        target="target:listing-1",
        resource_id="resource:price",
        network_host="EXAMPLE.TEST.",
    )
    spec = ToolSpec(
        tool_id="browser.inspect",
        description="inspect exact listing",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
    )
    call = ToolCall(
        call_id="effect-listing-1",
        tool_id=spec.tool_id,
        task_id=task_id,
        arguments={"field": "price", "locale": "uk-UA"},
    )
    handler_calls = 0

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return {"arguments": arguments, "price": 42}

    def executor(current_permissions: StandingPermissionStore) -> ToolExecutor:
        policy = StandingPermissionPolicy(
            current_permissions,
            binding,
            clock=lambda: start + timedelta(minutes=1),
        )
        current = ToolExecutor(
            approval_policy=policy,
            effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
        )
        current.register(spec, handler)
        return current

    first = asyncio.run(executor(permissions).execute(call))
    assert first.ok
    assert first.output == {"arguments": call.arguments, "price": 42}
    assert handler_calls == 1

    restarted_permissions = StandingPermissionStore(store, audit_log=AuditLog(store))
    restarted_permissions.initialize()
    replayed = asyncio.run(executor(restarted_permissions).execute(call))
    assert replayed.ok
    assert replayed.output == first.output
    assert handler_calls == 1

    swapped_arguments = asyncio.run(
        executor(restarted_permissions).execute(
            replace(call, arguments={"field": "title", "locale": "uk-UA"})
        )
    )
    assert not swapped_arguments.ok
    assert swapped_arguments.error == "tool effect not safe to execute"
    assert handler_calls == 1

    restarted_permissions.revoke(
        "perm-integrated",
        revoked_at=start + timedelta(minutes=2),
    )
    revoked_replay = asyncio.run(executor(restarted_permissions).execute(call))
    assert not revoked_replay.ok
    assert revoked_replay.error == "approval required"
    assert handler_calls == 1


def test_standing_policy_denies_changed_trusted_scope_task_and_high_impact(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "integrated-denials.db")
    store.initialize()
    task_id = TaskQueue(store).create(workspace_id="b06", agent_id="worker70").task_id
    context = PermissionContext(user_id="user-1", project_id="project-1", task_id=task_id)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    permissions.grant(
        permission_id="perm-integrated",
        scope=_scope(context=context, granted_at=start),
    )
    spec = ToolSpec(
        tool_id="browser.inspect",
        description="inspect exact listing",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
    )
    handler_calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return "unexpected"

    def executor(binding: StandingPermissionBinding, registered: ToolSpec) -> ToolExecutor:
        policy = StandingPermissionPolicy(
            permissions,
            binding,
            clock=lambda: start + timedelta(minutes=1),
        )
        current = ToolExecutor(
            approval_policy=policy,
            effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
        )
        current.register(registered, handler)
        return current

    changed_target = StandingPermissionBinding(
        permission_id="perm-integrated",
        subject_id="agent-parent",
        context=context,
        target="target:listing-2",
        resource_id="resource:price",
        network_host="example.test",
    )
    call = ToolCall(
        call_id="denied-target",
        tool_id=spec.tool_id,
        task_id=task_id,
        arguments={"field": "price"},
    )
    assert asyncio.run(executor(changed_target, spec).execute(call)).error == "approval required"

    exact_binding = replace(changed_target, target="target:listing-1")
    changed_task = replace(call, call_id="denied-task", task_id="task-other")
    assert asyncio.run(executor(exact_binding, spec).execute(changed_task)).error == (
        "approval required"
    )

    high_impact = replace(spec, risk=ToolRisk.HIGH_IMPACT)
    high_impact_call = replace(call, call_id="denied-high-impact")
    assert asyncio.run(executor(exact_binding, high_impact).execute(high_impact_call)).error == (
        "approval required"
    )
    assert handler_calls == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()


def test_child_agent_cannot_widen_parent_and_parent_revoke_cascades(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    parent = _scope(
        targets=("target:listing-1", "target:listing-2"),
        sites=("example.test", "api.example.test"),
        resources=("resource:price", "resource:title"),
        risk_ceiling=ToolRisk.LOCAL_WRITE,
        granted_at=start,
        expires_at=start + timedelta(hours=4),
    )
    permissions.grant(permission_id="perm-parent", scope=parent)

    child = _scope(
        subject_id="agent-child",
        targets=("target:listing-1",),
        sites=("example.test",),
        resources=("resource:price",),
        risk_ceiling=ToolRisk.READ_ONLY,
        granted_at=start + timedelta(minutes=1),
        expires_at=start + timedelta(hours=2),
    )
    permissions.delegate(
        parent_permission_id="perm-parent",
        permission_id="perm-child",
        scope=child,
        delegated_by_subject_id="agent-parent",
    )

    widened = (
        replace(child, action_class="browser.navigate"),
        replace(child, targets=("target:listing-3",)),
        replace(child, sites=("other.test",)),
        replace(child, resources=("resource:secret",)),
        replace(child, risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT),
        replace(child, context=PermissionContext("user-2", "project-1", "task-1")),
        replace(child, expires_at=parent.expires_at + timedelta(microseconds=1)),
    )
    for index, scope in enumerate(widened):
        with pytest.raises(PermissionError):
            permissions.delegate(
                parent_permission_id="perm-parent",
                permission_id=f"perm-wide-{index}",
                scope=scope,
                delegated_by_subject_id="agent-parent",
            )

    child_use = _use(
        subject_id="agent-child",
        risk=ToolRisk.READ_ONLY,
    )
    permissions.authorize("perm-child", child_use, now=start + timedelta(minutes=2))
    permissions.revoke("perm-parent", revoked_at=start + timedelta(minutes=3))
    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize("perm-child", child_use, now=start + timedelta(minutes=4))


def test_child_cannot_self_delegate_from_parent_identity(tmp_path) -> None:
    _store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    permissions.grant(permission_id="perm-parent", scope=_scope(granted_at=start))
    child = _scope(
        subject_id="agent-child",
        granted_at=start + timedelta(minutes=1),
        expires_at=start + timedelta(hours=1),
    )
    with pytest.raises(PermissionError, match="delegator"):
        permissions.delegate(
            parent_permission_id="perm-parent",
            permission_id="perm-child",
            scope=child,
            delegated_by_subject_id="agent-child",
        )


def test_audit_uses_safe_projection_without_raw_scoped_identities(tmp_path) -> None:
    _store, audit, permissions = _permissions(tmp_path)
    assert audit is not None
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    context = PermissionContext(
        user_id="user-canary-secret",
        project_id="project-canary-secret",
        task_id="task-canary-secret",
    )
    scope = _scope(
        subject_id="agent-canary-secret",
        context=context,
        targets=("target-canary-secret",),
        sites=("secret-canary.example.test",),
        resources=("resource-canary-secret",),
        granted_at=start,
    )
    permissions.grant(permission_id="perm-safe-audit", scope=scope)
    permissions.authorize(
        "perm-safe-audit",
        _use(
            subject_id="agent-canary-secret",
            context=context,
            target="target-canary-secret",
            site="secret-canary.example.test",
            resource_id="resource-canary-secret",
        ),
        now=start + timedelta(minutes=1),
    )
    permissions.revoke("perm-safe-audit", revoked_at=start + timedelta(minutes=2))

    rendered = json.dumps(
        [
            event.payload
            for event in audit.list_for(
                entity_type="standing_permission",
                entity_id="perm-safe-audit",
            )
        ],
        sort_keys=True,
    )
    for raw in (
        "user-canary-secret",
        "project-canary-secret",
        "task-canary-secret",
        "agent-canary-secret",
        "target-canary-secret",
        "secret-canary.example.test",
        "resource-canary-secret",
    ):
        assert raw not in rendered
    assert "scope_fingerprint" in rendered
    assert "target_count" in rendered
    assert "resource_count" in rendered


def test_durable_rows_do_not_store_raw_target_site_resource_or_context(tmp_path) -> None:
    store, _audit, permissions = _permissions(tmp_path)
    start = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    context = PermissionContext("user-private", "project-private", "task-private")
    permissions.grant(
        permission_id="perm-hashed",
        scope=_scope(
            subject_id="agent-private",
            context=context,
            targets=("target-private",),
            sites=("private.example.test",),
            resources=("resource-private",),
            granted_at=start,
        ),
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT * FROM standing_permissions WHERE permission_id = ?",
            ("perm-hashed",),
        ).fetchone()
    assert row is not None
    rendered = json.dumps(dict(row), sort_keys=True)
    for raw in (
        "user-private",
        "project-private",
        "task-private",
        "agent-private",
        "target-private",
        "private.example.test",
        "resource-private",
    ):
        assert raw not in rendered
