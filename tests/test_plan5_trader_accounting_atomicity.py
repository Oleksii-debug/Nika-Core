"""Trader paper-ledger admission and in-memory failure atomicity."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import nika_core.trading_research.accounting as accounting
from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import Instrument, TradingResearchError, Venue
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import (
    OrderAuthority,
    Side,
    SimulatedFill,
)

NOW = datetime(2026, 8, 26, tzinfo=UTC)
INSTRUMENT = Instrument("PLAN5-LEDGER", Venue("PAPER", "UTC"), "USD")


def _fill() -> SimulatedFill:
    return SimulatedFill(
        fill_id="plan5-fill",
        approval_id="approved",
        intent_id="intent",
        authority=OrderAuthority("workspace", "paper-run", "order", NOW, 0),
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity=Decimal(1),
        price=Decimal(100),
        fee=Decimal(1),
        filled_at=NOW,
        filled_slice=1,
    )


@pytest.mark.parametrize("cash", (Decimal("NaN"), Decimal("Infinity"), 1000.0, True))
def test_starting_capital_is_strict_finite_decimal(cash):
    with pytest.raises(TradingResearchError):
        PortfolioLedger(cash)


@pytest.mark.parametrize("field,invalid", (
    ("quantity", Decimal("Infinity")),
    ("quantity", Decimal("NaN")),
    ("price", Decimal("Infinity")),
    ("fee", Decimal("Infinity")),
    ("fee", Decimal(-1)),
    ("fee", 0.0),
))
def test_forged_fill_cannot_mutate_ledger_or_bind_workspace(field, invalid):
    ledger = PortfolioLedger(Decimal(1000))
    fill = _fill()
    object.__setattr__(fill, field, invalid)
    with pytest.raises(TradingResearchError):
        ledger.apply_fill(fill)
    assert ledger.cash == Decimal(1000)
    assert ledger.fees == Decimal(0)
    assert ledger._scope is None
    assert not ledger.has_applied_fill(fill.fill_id)


def test_position_calculation_failure_leaves_ledger_clean(monkeypatch):
    ledger = PortfolioLedger(Decimal(1000))

    def fail_update(*args):
        raise RuntimeError("injected position calculation failure")

    monkeypatch.setattr(accounting, "_apply_position_fill", fail_update)
    with pytest.raises(RuntimeError, match="injected position"):
        ledger.apply_fill(_fill())
    assert ledger.cash == Decimal(1000)
    assert ledger.fees == Decimal(0)
    assert ledger._scope is None
    assert not ledger.has_applied_fill("plan5-fill")

    monkeypatch.undo()
    ledger.apply_fill(_fill())
    assert ledger.cash == Decimal(899)
    assert ledger.fees == Decimal(1)
    assert ledger.has_applied_fill("plan5-fill")


@pytest.mark.parametrize("mark", (Decimal("NaN"), Decimal("Infinity"), 100.0))
def test_nonfinite_or_nondecimal_mark_cannot_create_account_snapshot(mark):
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(_fill())
    with pytest.raises(TradingResearchError):
        ledger.snapshot({instrument_identity(INSTRUMENT): mark})


def test_valid_mark_and_identical_replay_preserve_exactly_once_accounting():
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(_fill())
    first = ledger.snapshot({instrument_identity(INSTRUMENT): Decimal(100)})
    ledger.apply_fill(_fill())
    second = ledger.snapshot({instrument_identity(INSTRUMENT): Decimal(100)})
    assert first == second
    assert second.cash == Decimal(899)
    assert second.fees == Decimal(1)
    assert second.equity == Decimal(999)
