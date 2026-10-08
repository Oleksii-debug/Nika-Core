"""Plan 5 §1: real Core permission/revoke integration for read-only PAPER."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk
from nika_core.trading_research.paper_actions import (
    PAPER_ACCOUNT_INSPECT,
    paper_inspect_definition,
    paper_inspect_handler,
)
from nika_core.trading_research.paper_core_permission import standing_paper_read_authorizer
from nika_core.trading_research.workspace_query import PaperWorkspaceQuery
from nika_core.ui.bridge import UIActionBridge


def account():
    return {
        "cash": "98", "equity": "100", "gross_exposure": "2",
        "net_exposure": "2", "fees": "0", "realized_pnl": "0",
        "unrealized_pnl": "0", "positions": [],
    }


class PaperRepository:
    def __init__(self, *, during_read=None):
        self.reads = []
        self.during_read = during_read

    def account_payload(self, workspace_id, run_id):
        self.reads.append((workspace_id, run_id))
        if self.during_read is not None:
            self.during_read()
        return account()


def setup(tmp_path, *, grant=True, expired=False):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    context = PermissionContext("owner", "trader", "paper-session")
    binding = StandingPermissionBinding(
        permission_id="paper-grant",
        subject_id="owner-agent",
        context=context,
        target="workspace-a",
        resource_id="run-a",
        network_host=None,
    )
    now = datetime.now(UTC)
    if grant:
        permissions.grant(
            permission_id=binding.permission_id,
            scope=StandingPermissionScope(
                subject_id=binding.subject_id,
                context=binding.context,
                action_class=PAPER_ACCOUNT_INSPECT,
                targets=(binding.target,),
                sites=(),
                resources=(binding.resource_id,),
                risk_ceiling=ToolRisk.READ_ONLY,
                granted_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1) if expired else now + timedelta(hours=1),
            ),
        )
    return store, permissions, binding


def make_bridge(store, permissions, binding_resolver, repo, *, host_scope=lambda: ("workspace-a", "run-a")):
    registry = ActionRegistry()
    registry.register(paper_inspect_definition())
    query = PaperWorkspaceQuery(
        repo,
        authorize_read=standing_paper_read_authorizer(
            permissions, trusted_binding=binding_resolver
        ),
    )
    return UIActionBridge(
        registry,
        Keymap(store, registry),
        handlers={PAPER_ACCOUNT_INSPECT: paper_inspect_handler(query, host_scope=host_scope)},
    )


def inspect(bridge, payload=None):
    return bridge.dispatch({
        "request_id": "paper-read",
        "action_id": PAPER_ACCOUNT_INSPECT,
        "payload": {} if payload is None else payload,
    })


def test_core_grant_authorizes_paper_read_then_revocation_survives_restart(tmp_path):
    store, permissions, binding = setup(tmp_path)
    repo = PaperRepository()
    bridge = make_bridge(store, permissions, lambda: binding, repo)
    result = inspect(bridge)
    assert result["status"] == "completed"
    assert "Лише PAPER" in result["message"] and "100" in result["message"]
    assert repo.reads == [("workspace-a", "run-a")]
    permissions.revoke(binding.permission_id)
    rejected = inspect(bridge)
    assert rejected["status"] == "rejected"
    assert "Доступ" in rejected["message"]
    assert "100" not in rejected["message"]
    assert repo.reads == [("workspace-a", "run-a")]

    restarted = StandingPermissionStore(store)
    restarted.initialize()
    after_restart = inspect(make_bridge(store, restarted, lambda: binding, repo))
    assert after_restart["status"] == "rejected"
    assert repo.reads == [("workspace-a", "run-a")]


@pytest.mark.parametrize("variation", ["workspace", "run", "subject", "user", "project", "task", "permission", "network"])
def test_exact_core_scope_isolation_fails_closed(tmp_path, variation):
    store, permissions, binding = setup(tmp_path)
    repo = PaperRepository()
    host_scope = lambda: ("workspace-a", "run-a")
    if variation == "workspace":
        host_scope = lambda: ("workspace-b", "run-a")
    elif variation == "run":
        host_scope = lambda: ("workspace-a", "run-b")
    elif variation == "subject":
        binding = replace(binding, subject_id="other-agent")
    elif variation == "user":
        binding = replace(binding, context=replace(binding.context, user_id="other"))
    elif variation == "project":
        binding = replace(binding, context=replace(binding.context, project_id="other"))
    elif variation == "task":
        binding = replace(binding, context=replace(binding.context, task_id="other"))
    elif variation == "permission":
        binding = replace(binding, permission_id="unknown-permission")
    elif variation == "network":
        binding = replace(binding, network_host="example.test")
    result = inspect(make_bridge(store, permissions, lambda: binding, repo, host_scope=host_scope))
    assert result["status"] == "rejected"
    assert "100" not in result["message"]
    assert repo.reads == []


def test_missing_and_expired_authority_never_produce_account_read(tmp_path):
    for name, opts in (("missing", {"grant": False}), ("expired", {"expired": True})):
        store, permissions, binding = setup(tmp_path / name, **opts)
        repo = PaperRepository()
        result = inspect(make_bridge(store, permissions, lambda: binding, repo))
        assert result["status"] == "rejected"
        assert repo.reads == []


def test_host_resolution_failure_and_behavioral_binding_are_not_grants(tmp_path):
    store, permissions, binding = setup(tmp_path)
    repo = PaperRepository()

    def fault():
        raise RuntimeError("PRIVATE_PERMISSION_DETAIL")

    class HostileBinding(StandingPermissionBinding):
        pass

    for resolver in (fault, lambda: HostileBinding(
        permission_id=binding.permission_id, subject_id=binding.subject_id,
        context=binding.context, target=binding.target,
        resource_id=binding.resource_id, network_host=None,
    )):
        result = inspect(make_bridge(store, permissions, resolver, repo))
        assert result["status"] == "rejected"
        assert "PRIVATE_PERMISSION_DETAIL" not in result["message"]
        assert repo.reads == []


def test_mid_read_core_revocation_rechecked_before_visible_projection(tmp_path):
    store, permissions, binding = setup(tmp_path)
    repo = PaperRepository(during_read=lambda: permissions.revoke(binding.permission_id))
    result = inspect(make_bridge(store, permissions, lambda: binding, repo))
    assert result["status"] == "rejected"
    assert "Доступ" in result["message"]
    assert "100" not in result["message"]
    assert repo.reads == [("workspace-a", "run-a")]


def test_client_injection_cannot_choose_binding_or_create_trade(tmp_path):
    store, permissions, binding = setup(tmp_path)
    repo = PaperRepository()
    binding_calls = 0

    def trusted_binding():
        nonlocal binding_calls
        binding_calls += 1
        return binding

    bridge = make_bridge(store, permissions, trusted_binding, repo)
    for payload in (
        {"workspace_id": "workspace-b"},
        {"permission_id": "paper-grant"},
        {"authorized": True},
        {"execute_real_order": True},
    ):
        result = inspect(bridge, payload)
        assert result["status"] == "rejected"
    assert binding_calls == 0 and repo.reads == []
