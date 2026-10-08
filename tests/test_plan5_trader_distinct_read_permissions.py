"""Plan 5 §1: Core PAPER summary grant must not authorize detailed positions."""
from __future__ import annotations

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
    PAPER_POSITIONS_INSPECT,
    paper_inspect_definition,
    paper_inspect_handler,
    paper_positions_definition,
    paper_positions_handler,
)
from nika_core.trading_research.paper_core_permission import standing_paper_read_authorizer
from nika_core.trading_research.workspace_query import PaperWorkspaceQuery
from nika_core.ui.bridge import UIActionBridge


class Repository:
    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def account_payload(self, workspace: str, run: str) -> dict[str, object]:
        self.reads.append((workspace, run))
        return {
            "cash": "90", "equity": "100", "gross_exposure": "10",
            "net_exposure": "10", "fees": "0", "realized_pnl": "0",
            "unrealized_pnl": "0",
            "positions": [{
                "venue_id": "SIM", "venue_timezone": "UTC",
                "instrument_id": "TEST", "currency": "USD",
                "quantity": "1", "average_price": "10", "realized_pnl": "0",
            }],
        }


def _grant(
    permissions: StandingPermissionStore,
    context: PermissionContext,
    *,
    permission_id: str,
    action_id: str,
) -> StandingPermissionBinding:
    binding = StandingPermissionBinding(
        permission_id=permission_id,
        subject_id="owner-agent",
        context=context,
        target="trader-workspace",
        resource_id="paper-run",
        network_host=None,
    )
    now = datetime.now(UTC)
    permissions.grant(
        permission_id=permission_id,
        scope=StandingPermissionScope(
            subject_id=binding.subject_id,
            context=binding.context,
            action_class=action_id,
            targets=(binding.target,),
            sites=(),
            resources=(binding.resource_id,),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        ),
    )
    return binding


def _bridge(
    store: SQLiteStore,
    permissions: StandingPermissionStore,
    binding: StandingPermissionBinding,
    repo: Repository,
    *,
    action_id: str,
) -> UIActionBridge:
    authorizer = standing_paper_read_authorizer(
        permissions,
        trusted_binding=lambda: binding,
        action_id=action_id,
    )
    query = PaperWorkspaceQuery(repo, authorize_read=authorizer)
    host_scope = lambda: ("trader-workspace", "paper-run")
    registry = ActionRegistry()
    if action_id == PAPER_ACCOUNT_INSPECT:
        registry.register(paper_inspect_definition())
        handler = paper_inspect_handler(query, host_scope=host_scope)
    else:
        registry.register(paper_positions_definition())
        handler = paper_positions_handler(query, host_scope=host_scope)
    return UIActionBridge(
        registry,
        Keymap(store, registry),
        handlers={action_id: handler},
    )


def _dispatch(bridge: UIActionBridge, action_id: str) -> dict:
    return bridge.dispatch({
        "request_id": "paper-action-permission-check",
        "action_id": action_id,
        "payload": {},
    })


def test_paper_summary_grant_does_not_authorize_detailed_positions(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    context = PermissionContext("owner", "trader-project", "paper-task")
    account_binding = _grant(
        permissions, context,
        permission_id="summary-grant", action_id=PAPER_ACCOUNT_INSPECT,
    )
    repo = Repository()

    summary = _bridge(
        store, permissions, account_binding, repo,
        action_id=PAPER_ACCOUNT_INSPECT,
    )
    assert _dispatch(summary, PAPER_ACCOUNT_INSPECT)["status"] == "completed"
    assert repo.reads == [("trader-workspace", "paper-run")]

    # A host cannot accidentally reuse summary-only permission for detailed
    # position lines by changing only the registered UI command.
    positions = _bridge(
        store, permissions, account_binding, repo,
        action_id=PAPER_POSITIONS_INSPECT,
    )
    denied = _dispatch(positions, PAPER_POSITIONS_INSPECT)
    assert denied["status"] == "rejected"
    assert "заборонено" in denied["message"]
    assert "TEST" not in denied["message"]
    assert repo.reads == [("trader-workspace", "paper-run")]

    positions_binding = _grant(
        permissions, context,
        permission_id="positions-grant", action_id=PAPER_POSITIONS_INSPECT,
    )
    allowed = _bridge(
        store, permissions, positions_binding, repo,
        action_id=PAPER_POSITIONS_INSPECT,
    )
    accepted = _dispatch(allowed, PAPER_POSITIONS_INSPECT)
    assert accepted["status"] == "completed"
    assert "Лише PAPER" in accepted["message"]
    assert "TEST" in accepted["message"]
    assert repo.reads == [("trader-workspace", "paper-run")] * 2

    permissions.revoke("positions-grant")
    assert _dispatch(allowed, PAPER_POSITIONS_INSPECT)["status"] == "rejected"
    assert len(repo.reads) == 2

    # Exact grant restriction and revocation persist through SQLite reopen.
    reopened = StandingPermissionStore(store)
    reopened.initialize()
    assert _dispatch(
        _bridge(store, reopened, positions_binding, repo, action_id=PAPER_POSITIONS_INSPECT),
        PAPER_POSITIONS_INSPECT,
    )["status"] == "rejected"
    assert _dispatch(
        _bridge(store, reopened, account_binding, repo, action_id=PAPER_ACCOUNT_INSPECT),
        PAPER_ACCOUNT_INSPECT,
    )["status"] == "completed"
    assert len(repo.reads) == 3


@pytest.mark.parametrize(
    "action_id",
    ["trader.paper.execute", "trader.paper.*", "", None, 1, True],
)
def test_core_authorizer_rejects_unknown_action_before_grants(tmp_path, action_id) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    with pytest.raises(ValueError, match="unsupported PAPER read action"):
        standing_paper_read_authorizer(
            permissions,
            trusted_binding=lambda: None,
            action_id=action_id,
        )


def test_account_summary_default_action_remains_compatible(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    binding = _grant(
        permissions, PermissionContext("owner", "project", "task"),
        permission_id="default-account-grant", action_id=PAPER_ACCOUNT_INSPECT,
    )
    authorized = standing_paper_read_authorizer(
        permissions, trusted_binding=lambda: binding
    )
    assert authorized("trader-workspace", "paper-run") is True
    assert authorized("other-workspace", "paper-run") is False
