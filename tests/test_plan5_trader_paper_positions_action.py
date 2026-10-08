"""Plan 5 §1: NVDA-friendly PAPER positions paging through existing Core action."""
from __future__ import annotations

from collections.abc import Callable

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.trading_research.paper_actions import (
    PAPER_POSITIONS_INSPECT,
    paper_positions_definition,
    paper_positions_handler,
)
from nika_core.trading_research.workspace_query import PaperWorkspaceQuery
from nika_core.ui.bridge import UIActionBridge


class Repository:
    def __init__(self, payload: object = None) -> None:
        self.payload = payload
        self.reads: list[tuple[str, str]] = []

    def account_payload(self, workspace: str, run: str):
        self.reads.append((workspace, run))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def account(count: int) -> dict[str, object]:
    return {
        "cash": "100", "equity": "100", "gross_exposure": "0",
        "net_exposure": "0", "fees": "0", "realized_pnl": "0",
        "unrealized_pnl": "0",
        "positions": [
            {
                "venue_id": "SIM",
                "venue_timezone": "UTC" if index % 2 == 0 else "Europe/Bratislava",
                "instrument_id": f"ASSET{index}",
                "currency": "USD",
                "quantity": "1",
                "average_price": "2",
                "realized_pnl": "0",
            }
            for index in range(count)
        ],
    }


def build_bridge(
    tmp_path, repo: Repository, authorize: Callable[[str, str], bool],
    *, host_scope: Callable[[], tuple[str, str]] = lambda: ("mine", "run"),
) -> UIActionBridge:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    registry = ActionRegistry()
    registry.register(paper_positions_definition())
    return UIActionBridge(
        registry, Keymap(store, registry),
        handlers={
            PAPER_POSITIONS_INSPECT: paper_positions_handler(
                PaperWorkspaceQuery(repo, authorize_read=authorize),
                host_scope=host_scope,
            )
        },
    )


def dispatch(bridge: UIActionBridge, payload: dict | None = None) -> dict:
    return bridge.dispatch({
        "request_id": "positions-1",
        "action_id": PAPER_POSITIONS_INSPECT,
        "payload": {} if payload is None else payload,
    })


def test_semantic_remappable_action_and_bounded_keyboard_pages(tmp_path) -> None:
    repo = Repository(account(12))
    checks: list[tuple[str, str]] = []

    def authorized(workspace: str, run: str) -> bool:
        checks.append((workspace, run))
        return (workspace, run) == ("mine", "run")

    bridge = build_bridge(tmp_path, repo, authorized)
    listed = bridge.list_actions()
    assert len(listed) == 1
    assert listed[0]["action_id"] == PAPER_POSITIONS_INSPECT
    assert listed[0]["label"] == "Переглянути PAPER-позиції"
    assert listed[0]["may_be_unbound"] is True
    assert bridge.set_binding(PAPER_POSITIONS_INSPECT, "Ctrl+Alt+P")["ok"] is True
    assert bridge.list_actions()[0]["binding"] == "Ctrl+Alt+P"

    first = dispatch(bridge)
    assert first["status"] == "completed"
    assert "позиції 1–10 із 12" in first["message"]
    assert "Позиція 1:" in first["message"]
    assert "Позиція 11:" not in first["message"]
    assert "часовий пояс UTC" in first["message"]
    assert "Europe/Bratislava" in first["message"]
    assert "наступних позицій" in first["message"]

    second = dispatch(bridge, {"page": 1})
    assert second["status"] == "completed"
    assert "позиції 11–12 із 12" in second["message"]
    assert "ASSET10" in second["message"]
    assert "ASSET0" not in second["message"]
    assert len(second["message"].encode("utf-8")) < 32_768
    assert checks == [("mine", "run")] * 4
    assert repo.reads == [("mine", "run")] * 2


@pytest.mark.parametrize(
    "bad",
    [
        {"workspace_id": "foreign"},
        {"run_id": "foreign"},
        {"authorized": True},
        {"execute_real_order": True},
        {"page": True},
        {"page": -1},
        {"page": 10001},
        {"page": "1"},
        {"page": 0, "run_id": "foreign"},
    ],
)
def test_page_cannot_select_scope_grant_or_real_trade(tmp_path, bad) -> None:
    repo = Repository(account(1))
    checks: list[int] = []
    bridge = build_bridge(tmp_path, repo, lambda _w, _r: checks.append(1) or True)
    result = dispatch(bridge, bad)
    assert result["status"] == "rejected"
    assert repo.reads == []
    assert checks == []
    assert "foreign" not in result["message"]


def test_each_page_rechecks_revocable_core_permission(tmp_path) -> None:
    repo = Repository(account(12))
    allowed = True

    def authorize(workspace: str, run: str) -> bool:
        return allowed and (workspace, run) == ("mine", "run")

    bridge = build_bridge(tmp_path, repo, authorize)
    assert dispatch(bridge)["status"] == "completed"
    allowed = False
    denied = dispatch(bridge, {"page": 1})
    assert denied["status"] == "rejected"
    assert "заборонено" in denied["message"]
    assert "ASSET" not in denied["message"]
    assert repo.reads == [("mine", "run")]


def test_mid_read_revocation_is_not_disclosed_as_evidence_failure(tmp_path) -> None:
    repo = Repository(RuntimeError("SECRET_POSITION_123"))
    counter = 0

    def authorize(_w: str, _r: str) -> bool:
        nonlocal counter
        counter += 1
        return counter == 1

    bridge = build_bridge(tmp_path, repo, authorize)
    result = dispatch(bridge)
    assert result["status"] == "rejected"
    assert "заборонено" in result["message"]
    assert "SECRET" not in result["message"]
    assert counter == 2 and repo.reads == [("mine", "run")]


def test_corrupt_positions_are_never_returned_or_converted_to_empty(tmp_path) -> None:
    corrupted = account(1)
    corrupted["positions"][0]["quantity"] = "NaN"
    repo = Repository(corrupted)
    result = dispatch(build_bridge(tmp_path, repo, lambda _w, _r: True))
    assert result["status"] == "rejected"
    assert "недоступні" in result["message"]
    assert "NaN" not in result["message"]


def test_empty_positions_and_pages_remain_explicit(tmp_path) -> None:
    repo = Repository(None)
    bridge = build_bridge(tmp_path, repo, lambda _w, _r: True)
    assert "записів рахунку поки немає" in dispatch(bridge)["message"]
    assert dispatch(bridge, {"page": 1})["status"] == "rejected"

    repo.payload = account(0)
    assert "відкритих позицій немає" in dispatch(bridge)["message"]
    assert dispatch(bridge, {"page": 1})["status"] == "rejected"

    repo.payload = account(2)
    assert dispatch(bridge, {"page": 1})["status"] == "rejected"


def test_restarted_keymap_keeps_canonical_action_identity(tmp_path) -> None:
    repo = Repository(account(1))
    bridge = build_bridge(tmp_path, repo, lambda _w, _r: True)
    assert bridge.set_binding(PAPER_POSITIONS_INSPECT, "Ctrl+Alt+P")["ok"]
    restarted = build_bridge(tmp_path, repo, lambda _w, _r: True)
    assert restarted.list_actions()[0]["binding"] == "Ctrl+Alt+P"
    assert "ASSET0" in dispatch(restarted)["message"]
