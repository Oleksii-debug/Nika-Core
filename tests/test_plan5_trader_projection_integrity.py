"""Plan 5 §1: account evidence must remain inert at the Core/UI projection edge."""
from __future__ import annotations

from decimal import Decimal

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


class HostileText(str):
    def __str__(self) -> str:
        raise AssertionError("untrusted text hook was executed")

    def isprintable(self) -> bool:
        raise AssertionError("untrusted printability hook was executed")


def clean_account() -> dict[str, object]:
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


def projected(repo: Repository, *, revocation: bool = False):
    checks = 0

    def authorize(_w: str, _r: str) -> bool:
        nonlocal checks
        checks += 1
        return not revocation or checks == 1

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    state = paper_state_provider(query, host_scope=lambda: ("mine", "run"))()
    return state, checks


def test_legitimate_paper_projection_is_text_first_and_authorized_twice() -> None:
    repo = Repository(clean_account())
    state, checks = projected(repo)
    assert checks == 2
    assert repo.reads == 1
    assert state["state"] == "PAPER_DATA"
    assert state["mode"] == "PAPER_ONLY"
    assert state["equity"] == "100"
    assert state["cash"] == "98"
    assert state["positions"] == [{
        "venue": "SIM", "instrument": "TEST", "currency": "USD",
        "quantity": "1", "average_price": "2", "realized_pnl": "0",
    }]


@pytest.mark.parametrize(
    "bad", [
        object(),
        Decimal("1"),
        HostileText("1"),
        "NaN",
        "-Infinity",
        " 1",
        "1 ",
        "1\\n2",
        "not-a-decimal",
        "9" * 129,
    ],
)
@pytest.mark.parametrize(
    "field", [
        "cash", "equity", "gross_exposure", "net_exposure",
        "fees", "realized_pnl", "unrealized_pnl",
    ],
)
def test_bad_account_amounts_are_denied_to_ui_as_unavailable(field: str, bad: object) -> None:
    payload = clean_account()
    payload[field] = bad
    repo = Repository(payload)
    state, checks = projected(repo)
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert "cash" not in state and "positions" not in state
    assert checks == 2 and repo.reads == 1


@pytest.mark.parametrize(
    "bad", [
        object(), Decimal(1), HostileText("1"), "NaN",
        "Infinity", " 1", "1\\n2", "x" * 129,
    ],
)
@pytest.mark.parametrize("field", ["quantity", "average_price", "realized_pnl"])
def test_bad_position_amounts_never_leave_canonical_projection(
    field: str, bad: object,
) -> None:
    payload = clean_account()
    payload["positions"][0][field] = bad
    state, checks = projected(Repository(payload))
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert "positions" not in state
    assert checks == 2


@pytest.mark.parametrize(
    "identity", [
        " SIM", "SIM ", "SIM\\nforged", "SIM\\u202eforged",
        "X" * 513, HostileText("SIM"), 7,
    ],
)
def test_unbounded_behavioral_or_spoofed_operator_identity_never_renders(identity: object):
    payload = clean_account()
    payload["positions"][0]["venue_id"] = identity
    state, checks = projected(Repository(payload))
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert checks == 2


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "ЇЇЇ", "U\\u202eD"])
def test_currency_must_be_unambiguous_ascii_code(currency: str) -> None:
    payload = clean_account()
    payload["positions"][0]["currency"] = currency
    state, checks = projected(Repository(payload))
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert checks == 2


def test_untrusted_payload_shape_never_becomes_paper_data() -> None:
    for payload in (
        {"positions": []},
        {**clean_account(), "secret": "unsafe"},
        [clean_account()],
        {**clean_account(), "positions": "corrupted"},
        {**clean_account(), "positions": [clean_account()["positions"][0]] * 100_001},
    ):
        state, checks = projected(Repository(payload))
        assert state["state"] == "EVIDENCE_UNAVAILABLE"
        assert "cash" not in state
        assert checks == 2


def test_revocation_on_projection_failure_dominates_health_disclosure() -> None:
    payload = clean_account()
    payload["cash"] = "NaN"
    repo = Repository(payload)
    state, checks = projected(repo, revocation=True)
    assert state["state"] == "ACCESS_DENIED"
    assert "cash" not in state and "positions" not in state
    assert checks == 2 and repo.reads == 1
