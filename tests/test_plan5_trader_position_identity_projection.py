"""Plan 5 §1: re-admit complete PAPER position identity before UI projection.

These tests exercise a substituted faulty read adapter. Canonical SQLite decoding
already validates its rows; the presentation fence must not trust that a
different adapter preserved identity integrity or inert plain values.
"""
from __future__ import annotations

import pytest

from nika_core.trading_research.workspace_query import (
    PaperWorkspaceQuery,
    paper_state_provider,
)


class Repository:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.reads = 0

    def account_payload(self, _workspace: str, _run: str) -> object:
        self.reads += 1
        return self.payload


def account() -> dict[str, object]:
    return {
        "cash": "98",
        "equity": "100",
        "gross_exposure": "2",
        "net_exposure": "2",
        "fees": "0",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "positions": [{
            "venue_id": "SIM",
            "venue_timezone": "UTC",
            "instrument_id": "TEST",
            "currency": "USD",
            "quantity": "1",
            "average_price": "2",
            "realized_pnl": "0",
        }],
    }


def project(payload: object, *, revoke: bool = False) -> tuple[dict[str, object], int, int]:
    repo = Repository(payload)
    calls = 0

    def authorize(_workspace: str, _run: str) -> bool:
        nonlocal calls
        calls += 1
        return not revoke or calls == 1

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    state = paper_state_provider(query, host_scope=lambda: ("workspace", "run"))()
    return state, calls, repo.reads


class BehavioralTimezone(str):
    def __str__(self) -> str:
        raise AssertionError("foreign timezone __str__ invoked")

    def isprintable(self) -> bool:
        raise AssertionError("foreign timezone isprintable invoked")


@pytest.mark.parametrize("bad", [
    None, 7, "", " UTC", "UTC ", "UTC\nforged", "UTC\u202eforged",
    "X" * 513, BehavioralTimezone("UTC"),
])
@pytest.mark.parametrize("revoke", [False, True])
def test_invalid_or_behavioral_hidden_timezone_is_fail_closed(
    bad: object, revoke: bool,
) -> None:
    payload = account()
    payload["positions"][0]["venue_timezone"] = bad
    state, calls, reads = project(payload, revoke=revoke)
    assert calls == 2 and reads == 1
    assert state["mode"] == "PAPER_ONLY"
    assert state["state"] == ("ACCESS_DENIED" if revoke else "EVIDENCE_UNAVAILABLE")
    assert "cash" not in state and "positions" not in state


@pytest.mark.parametrize("revoke", [False, True])
def test_duplicate_position_identity_cannot_inflate_accessible_holdings(
    revoke: bool,
) -> None:
    payload = account()
    duplicate = dict(payload["positions"][0])
    duplicate["quantity"] = "999"
    payload["positions"].append(duplicate)
    state, calls, reads = project(payload, revoke=revoke)
    assert calls == 2 and reads == 1
    assert state["state"] == ("ACCESS_DENIED" if revoke else "EVIDENCE_UNAVAILABLE")
    assert "positions" not in state and "equity" not in state


def test_venue_timezone_is_part_of_canonical_position_identity() -> None:
    payload = account()
    other = dict(payload["positions"][0])
    other["venue_timezone"] = "Europe/Bratislava"
    payload["positions"].append(other)
    state, calls, reads = project(payload)
    assert calls == 2 and reads == 1
    assert state["state"] == "PAPER_DATA"
    assert len(state["positions"]) == 2
    assert all(row["instrument"] == "TEST" for row in state["positions"])
    assert [row["venue_timezone"] for row in state["positions"]] == [
        "UTC", "Europe/Bratislava",
    ]
    assert len({(row["venue"], row["venue_timezone"], row["instrument"], row["currency"])
                for row in state["positions"]}) == 2


def test_single_position_projection_still_works_without_new_authority() -> None:
    state, calls, reads = project(account())
    assert (state["state"], calls, reads) == ("PAPER_DATA", 2, 1)
    assert state["positions"][0]["venue"] == "SIM"
    assert state["positions"][0]["venue_timezone"] == "UTC"
