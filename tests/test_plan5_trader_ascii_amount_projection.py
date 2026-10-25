"""Plan 5 §1: no mixed-script or noncanonical numbers in PAPER UI evidence."""
from __future__ import annotations

import pytest

from nika_core.trading_research.workspace_query import (
    PaperWorkspaceQuery,
    paper_state_provider,
)


def paper_payload() -> dict[str, object]:
    return {
        "cash": "98", "equity": "100", "gross_exposure": "2",
        "net_exposure": "2", "fees": "0", "realized_pnl": "0",
        "unrealized_pnl": "0",
        "positions": [{
            "venue_id": "SIM", "venue_timezone": "UTC",
            "instrument_id": "TEST", "currency": "USD",
            "quantity": "1", "average_price": "2", "realized_pnl": "0",
        }],
    }


class Adapter:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.reads = 0

    def account_payload(self, workspace: str, run: str) -> object:
        assert (workspace, run) == ("host-owned", "paper-run")
        self.reads += 1
        return self.payload


def render(payload: dict[str, object], *, revoke: bool = False):
    adapter = Adapter(payload)
    calls = 0

    def authorize(workspace: str, run: str) -> bool:
        nonlocal calls
        assert (workspace, run) == ("host-owned", "paper-run")
        calls += 1
        return not revoke or calls == 1

    query = PaperWorkspaceQuery(adapter, authorize_read=authorize)
    state = paper_state_provider(
        query, host_scope=lambda: ("host-owned", "paper-run")
    )()
    return state, calls, adapter.reads


@pytest.mark.parametrize("field", [
    "cash", "equity", "gross_exposure", "net_exposure",
    "fees", "realized_pnl", "unrealized_pnl",
])
@pytest.mark.parametrize("unsafe", [
    "١٢",          # Arabic-Indic digits
    "１２",          # fullwidth digits
    "1٢",          # mixed-script digits
    "١.5E+2",      # mixed-script exponent input
    "+1",          # not a canonical Decimal.__str__ projection
])
@pytest.mark.parametrize("revoke", [False, True])
def test_unsafe_account_number_never_discloses_evidence(
    field: str, unsafe: str, revoke: bool,
) -> None:
    payload = paper_payload()
    payload[field] = unsafe
    state, checks, reads = render(payload, revoke=revoke)
    assert state["state"] == ("ACCESS_DENIED" if revoke else "EVIDENCE_UNAVAILABLE")
    assert state["mode"] == "PAPER_ONLY"
    assert "cash" not in state and "positions" not in state
    assert checks == 2 and reads == 1


@pytest.mark.parametrize("field", ["quantity", "average_price", "realized_pnl"])
@pytest.mark.parametrize("unsafe", ["٠", "１.0", "1٣", "+1"])
def test_unsafe_position_number_rejected_before_ui(
    field: str, unsafe: str,
) -> None:
    payload = paper_payload()
    payload["positions"][0][field] = unsafe
    state, checks, reads = render(payload)
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert "cash" not in state and "positions" not in state
    assert checks == 2 and reads == 1


@pytest.mark.parametrize("canonical", [
    "0", "-0", "0.000", "-0.000", "1E+3", "1E-3", "0E-10", "1.25",
])
def test_ascii_canonical_decimal_remains_text_first(canonical: str) -> None:
    payload = paper_payload()
    payload["cash"] = canonical
    payload["positions"][0]["quantity"] = canonical
    state, checks, reads = render(payload)
    assert state["state"] == "PAPER_DATA"
    assert state["mode"] == "PAPER_ONLY"
    assert state["cash"] == canonical
    assert state["positions"][0]["quantity"] == canonical
    assert checks == 2 and reads == 1
