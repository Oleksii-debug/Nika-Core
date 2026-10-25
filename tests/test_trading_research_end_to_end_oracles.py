from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import EventTime, Instrument, Quote, Venue
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderType,
    Side,
)
from nika_core.trading_research.replay import SimulationExecutionEngine, TimeSlice
from nika_core.trading_research.risk import RiskEngine, RiskLimits, RiskState

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trading_research_end_to_end_oracles.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_CASES = _FIXTURE["cases"]
_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_INSTRUMENT = Instrument("E2E", Venue("SIM-E2E", "UTC"), "USD")
_IDENTITY = instrument_identity(_INSTRUMENT)


def test_end_to_end_oracle_fixture_is_exactly_32_unique_cases() -> None:
    assert _FIXTURE["schema_version"] == 1
    assert len(_CASES) == 32
    ids = [str(case["id"]) for case in _CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: str(case["id"]))
def test_hand_recalculable_end_to_end_oracle(case: dict[str, object]) -> None:
    starting_cash = _decimal(case["starting_cash"])
    ledger = PortfolioLedger(starting_cash)
    risk = _risk_engine()
    execution = SimulationExecutionEngine()
    steps = case["steps"]
    assert isinstance(steps, list)
    assert steps

    for index, raw_step in enumerate(steps):
        assert isinstance(raw_step, dict)
        step = raw_step
        side = Side(str(step["side"]))
        order_type = OrderType(str(step.get("order_type", "market")))
        quantity = _decimal(step["quantity"])
        bid = _decimal(step["bid"])
        ask = _decimal(step["ask"])
        risk_mark = ask if side is Side.BUY else bid
        submitted_slice = index * 2
        submitted_at = _NOW + timedelta(minutes=submitted_slice)
        authority = OrderAuthority(
            "oracle-workspace",
            f"oracle-{case['id']}",
            f"order-{index + 1}",
            submitted_at,
            submitted_slice,
        )
        intent = OrderIntent(
            intent_id=f"proposal-{case['id']}-{index + 1}",
            instrument=_INSTRUMENT,
            side=side,
            order_type=order_type,
            quantity=quantity,
            submitted_at=submitted_at,
            submitted_slice=submitted_slice,
            limit_price=(
                _decimal(step["limit_price"])
                if step.get("limit_price") is not None
                else None
            ),
        )
        policy = ExecutionPolicy(
            policy_id=f"oracle-policy-{case['id']}-{index + 1}",
            slippage_bps=_decimal(step.get("slippage_bps", "0")),
            fee_bps=_decimal(step.get("fee_bps", "0")),
            fixed_fee=_decimal(step.get("fixed_fee", "0")),
            max_fill_fraction=_decimal(step.get("max_fill_fraction", "1")),
        )
        before = ledger.snapshot({_IDENTITY: risk_mark})
        risk_state = RiskState(
            peak_equity=max(starting_cash, before.equity),
            session_start_equity=starting_cash,
        )
        approved = risk.approve(
            intent,
            authority=authority,
            snapshot=before,
            mark_price=risk_mark,
            pending_signed_quantity=Decimal(0),
            approved_at=submitted_at,
            approved_slice=submitted_slice,
            policy=policy,
            risk_state=risk_state,
        )

        execution_at = submitted_at + timedelta(minutes=1)
        quote = Quote(
            _INSTRUMENT,
            EventTime(execution_at, execution_at, execution_at),
            bid,
            ask,
            _decimal(step.get("bid_size", "1000")),
            _decimal(step.get("ask_size", "1000")),
            index + 1,
        )
        update = execution.execute(
            approved,
            TimeSlice(submitted_slice + 1, execution_at, (quote,)),
        )

        assert update.fill is not None
        assert update.fill.quantity == _decimal(step["expected_fill_quantity"])
        assert update.fill.price == _decimal(step["expected_fill_price"])
        assert update.fill.fee == _decimal(step["expected_fee"])
        assert update.fill.authority == authority

        ledger.apply_fill(update.fill)
        after = ledger.snapshot({_IDENTITY: risk_mark})
        risk.assert_post_fill(after, risk_state)

    final_mark = _decimal(case["final_mark"])
    snapshot = ledger.snapshot({_IDENTITY: final_mark})
    position = ledger.position(_INSTRUMENT)
    expected = case["expected"]
    assert isinstance(expected, dict)

    assert snapshot.cash == _decimal(expected["cash"])
    assert snapshot.fees == _decimal(expected["fees"])
    assert position.quantity == _decimal(expected["quantity"])
    assert position.average_price == _decimal(expected["average_price"])
    assert position.realized_pnl == _decimal(expected["realized_pnl"])
    assert snapshot.realized_pnl == _decimal(expected["realized_pnl"])
    assert snapshot.unrealized_pnl == _decimal(expected["unrealized_pnl"])
    assert snapshot.equity == _decimal(expected["equity"])
    assert snapshot.gross_exposure == _decimal(expected["gross_exposure"])
    assert snapshot.net_exposure == _decimal(expected["net_exposure"])


def _risk_engine() -> RiskEngine:
    return RiskEngine(
        RiskLimits(
            max_abs_position=Decimal(1000),
            max_gross_exposure=Decimal(1_000_000),
            max_net_exposure=Decimal(1_000_000),
            max_session_loss=Decimal(1_000_000),
            max_drawdown=Decimal(1_000_000),
            allow_short=True,
            max_leverage=Decimal(1000),
        )
    )


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))
