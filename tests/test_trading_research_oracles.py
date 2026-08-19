from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nika_core.trading_research.accounting import AccountSnapshot, PortfolioLedger
from nika_core.trading_research.contracts import Instrument, Venue
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderType,
    Side,
    SimulatedFill,
    apply_slippage,
    fee_for,
)
from nika_core.trading_research.risk import RiskEngine, RiskLimits, RiskRejected, RiskState

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")
FIXTURE = Path(__file__).parent / "fixtures" / "trading_research_numerical_oracles.json"


def _cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


CASES = _cases()


def test_oracle_fixture_contains_exactly_42_cases() -> None:
    assert len(CASES) == 42
    assert len({str(case["id"]) for case in CASES}) == 42


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["id"]))
def test_numerical_oracle(case: dict[str, object]) -> None:
    kind = str(case["kind"])
    if kind == "fee":
        policy = ExecutionPolicy(
            "oracle",
            fee_bps=Decimal(str(case["fee_bps"])),
            fixed_fee=Decimal(str(case["fixed_fee"])),
        )
        assert fee_for(Decimal(str(case["notional"])), policy) == Decimal(str(case["expected"]))
        return
    if kind == "slippage":
        side = Side(str(case["side"]))
        actual = apply_slippage(
            Decimal(str(case["price"])), side, Decimal(str(case["bps"]))
        )
        assert actual == Decimal(str(case["expected"]))
        return
    if kind == "account":
        _assert_account_oracle(case)
        return
    if kind == "risk":
        _assert_risk_oracle(case)
        return
    raise AssertionError(f"unknown oracle kind: {kind}")


def _assert_account_oracle(case: dict[str, object]) -> None:
    ledger = PortfolioLedger(Decimal(str(case["starting_cash"])))
    fills = case["fills"]
    assert isinstance(fills, list)
    for index, raw in enumerate(fills):
        assert isinstance(raw, dict)
        side = Side(str(raw["side"]))
        ledger.apply_fill(
            SimulatedFill(
                fill_id=f"oracle:{case['id']}:{index}",
                approval_id="approval",
                intent_id="intent",
                instrument=INSTRUMENT,
                side=side,
                quantity=Decimal(str(raw["qty"])),
                price=Decimal(str(raw["price"])),
                fee=Decimal(str(raw["fee"])),
                filled_at=NOW,
                filled_slice=index + 1,
            )
        )
    snapshot = ledger.snapshot({INSTRUMENT.instrument_id: Decimal(str(case["mark"]))})
    position = ledger.position(INSTRUMENT)
    expected = case["expected"]
    assert isinstance(expected, dict)
    assert snapshot.cash == Decimal(str(expected["cash"]))
    assert position.quantity == Decimal(str(expected["quantity"]))
    assert position.average_price == Decimal(str(expected["average_price"]))
    assert snapshot.realized_pnl == Decimal(str(expected["realized_pnl"]))
    assert snapshot.fees == Decimal(str(expected["fees"]))
    assert snapshot.equity == Decimal(str(expected["equity"]))


def _assert_risk_oracle(case: dict[str, object]) -> None:
    limits = RiskLimits(
        max_abs_position=Decimal(str(case["max_abs_position"])),
        max_gross_exposure=Decimal(str(case["max_gross"])),
        max_net_exposure=Decimal(str(case["max_net"])),
        max_session_loss=Decimal(str(case["max_session_loss"])),
        max_drawdown=Decimal(str(case["max_drawdown"])),
        allow_short=bool(case["allow_short"]),
    )
    engine = RiskEngine(limits)
    intent = OrderIntent(
        intent_id=str(case["id"]),
        instrument=INSTRUMENT,
        side=Side(str(case["side"])),
        order_type=OrderType.MARKET,
        quantity=Decimal(str(case["qty"])),
        submitted_at=NOW,
        submitted_slice=0,
    )
    snapshot = AccountSnapshot(
        cash=Decimal(1000),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )
    kwargs = dict(
        snapshot=snapshot,
        mark_price=Decimal(str(case["mark"])),
        pending_signed_quantity=Decimal(str(case["pending"])),
        approved_at=NOW,
        approved_slice=0,
        policy=ExecutionPolicy("oracle"),
        risk_state=RiskState(peak_equity=Decimal(1000), session_start_equity=Decimal(1000)),
    )
    outcome = str(case["outcome"])
    if outcome.startswith("allow"):
        approved = engine.approve(intent, **kwargs)
        assert approved.intent is intent
    else:
        with pytest.raises(RiskRejected):
            engine.approve(intent, **kwargs)
