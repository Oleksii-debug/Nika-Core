"""Plan 5 §1: paper account command uses incumbent ActionRegistry and Core query."""
from __future__ import annotations

from collections.abc import Callable

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.trading_research.paper_actions import (
    PAPER_ACCOUNT_INSPECT,
    paper_inspect_definition,
    paper_inspect_handler,
)
from nika_core.trading_research.workspace_query import PaperWorkspaceQuery
from nika_core.ui.bridge import UIActionBridge


class PaperRepository:
    def __init__(self, payload: object = None) -> None:
        self.payload = payload
        self.reads: list[tuple[str, str]] = []

    def account_payload(self, workspace_id: str, run_id: str):
        self.reads.append((workspace_id, run_id))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def account() -> dict[str, object]:
    return {
        "cash": "98", "equity": "100", "gross_exposure": "2",
        "net_exposure": "2", "fees": "0", "realized_pnl": "0",
        "unrealized_pnl": "0", "positions": [{
            "venue_id": "SIM", "venue_timezone": "UTC",
            "instrument_id": "TEST", "currency": "USD",
            "quantity": "1", "average_price": "2", "realized_pnl": "0",
        }],
    }


def bridge_for(
    tmp_path,
    repo: PaperRepository,
    authorize: Callable[[str, str], bool],
    host_scope: Callable[[], tuple[str, str]] = lambda: ("mine", "run"),
):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    registry = ActionRegistry()
    registry.register(paper_inspect_definition())
    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    bridge = UIActionBridge(
        registry,
        Keymap(store, registry),
        handlers={
            PAPER_ACCOUNT_INSPECT: paper_inspect_handler(query, host_scope=host_scope),
        },
    )
    return bridge


def dispatch(bridge: UIActionBridge, payload=None) -> dict:
    return bridge.dispatch({
        "request_id": "read-1",
        "action_id": PAPER_ACCOUNT_INSPECT,
        "payload": {} if payload is None else payload,
    })


def test_registered_action_is_semantic_keyboard_remappable_and_paper_only(tmp_path) -> None:
    repo = PaperRepository(account())
    decisions: list[tuple[str, str]] = []
    def allow(workspace: str, run: str) -> bool:
        decisions.append((workspace, run))
        return (workspace, run) == ("mine", "run")

    bridge = bridge_for(tmp_path, repo, allow)
    listed = {a["action_id"]: a for a in bridge.list_actions()}
    assert PAPER_ACCOUNT_INSPECT in listed
    assert listed[PAPER_ACCOUNT_INSPECT]["label"] == "Перевірити PAPER-рахунок"
    assert listed[PAPER_ACCOUNT_INSPECT]["binding"] is None
    assert listed[PAPER_ACCOUNT_INSPECT]["may_be_unbound"] is True
    assert bridge.set_binding(PAPER_ACCOUNT_INSPECT, "Ctrl+Alt+P")["ok"] is True
    assert bridge.list_actions()[0]["binding"] == "Ctrl+Alt+P"
    result = dispatch(bridge)
    assert result["status"] == "completed"
    assert result["request_id"] == "read-1"
    assert "Лише PAPER" in result["message"]
    assert "100" in result["message"] and "позицій 1" in result["message"]
    assert decisions == [("mine", "run"), ("mine", "run")]
    assert repo.reads == [("mine", "run")]


def test_empty_account_is_explicit_no_data_not_a_fabricated_zero(tmp_path) -> None:
    result = dispatch(bridge_for(tmp_path, PaperRepository(), lambda _w, _r: True))
    assert result["status"] == "completed"
    assert "записів рахунку поки немає" in result["message"]
    assert "капітал 0" not in result["message"]


@pytest.mark.parametrize(
    "payload", [
        {"workspace_id": "foreign"},
        {"run_id": "elsewhere"},
        {"authorized": True},
        {"execute_real_order": True},
    ],
)
def test_ui_cannot_select_tenant_grant_or_real_trade(tmp_path, payload) -> None:
    repo = PaperRepository(account())
    calls = 0

    def allow(_w: str, _r: str) -> bool:
        nonlocal calls
        calls += 1
        return True

    result = dispatch(bridge_for(tmp_path, repo, allow), payload)
    assert result["status"] == "rejected"
    assert "не приймає параметрів" in result["message"]
    assert repo.reads == [] and calls == 0


def test_foreign_scope_and_untrusted_host_fail_closed_without_leaks(tmp_path) -> None:
    repo = PaperRepository(account())
    bridge = bridge_for(
        tmp_path, repo, lambda ws, _r: ws == "mine",
        host_scope=lambda: ("foreign", "run"),
    )
    denied = dispatch(bridge)
    assert denied["status"] == "rejected"
    assert "Доступ" in denied["message"]
    assert repo.reads == []
    malformed = bridge_for(
        tmp_path, repo, lambda _w, _r: True,
        host_scope=lambda: ("mine", "run", "foreign"),
    )
    assert dispatch(malformed)["status"] == "rejected"
    assert repo.reads == []


def test_revocation_during_read_dominates_evidence_health(tmp_path) -> None:
    repo = PaperRepository(RuntimeError("PRIVATE_STORAGE_DETAIL"))
    checks = 0

    def allow(_w: str, _r: str) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    result = dispatch(bridge_for(tmp_path, repo, allow))
    assert result["status"] == "rejected"
    assert "Доступ" in result["message"]
    assert "PRIVATE" not in result["message"]
    assert checks == 2 and repo.reads == [("mine", "run")]


def test_corrupt_evidence_never_exports_provider_exception_or_balances(tmp_path) -> None:
    repo = PaperRepository(RuntimeError("SECRET_BALANCE_123"))
    result = dispatch(bridge_for(tmp_path, repo, lambda _w, _r: True))
    assert result["status"] == "rejected"
    assert "недоступні" in result["message"]
    assert "SECRET" not in result["message"]
    assert "123" not in result["message"]


def test_missing_action_does_not_run_an_alternative_agent(tmp_path) -> None:
    repo = PaperRepository(account())
    bridge = bridge_for(tmp_path, repo, lambda _w, _r: True)
    response = bridge.dispatch({
        "request_id": "x",
        "action_id": "trader.paper.account.trade_live",
        "payload": {},
    })
    assert response["status"] == "rejected"
    assert repo.reads == []
