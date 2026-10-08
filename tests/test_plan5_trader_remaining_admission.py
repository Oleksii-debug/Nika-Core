"""Plan 5 section 1: remaining quantity must never exceed risk-approved intent."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research.contracts import (
    EventTime,
    Instrument,
    Quote,
    TradingResearchError,
    Venue,
)
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
)
from nika_core.trading_research.replay import (
    SimulationExecutionEngine,
    TimeSlice,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("PAPER", "UTC"), "USD")


def _approved_order() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        "approval",
        OrderIntent(
            "intent", INSTRUMENT, Side.BUY, OrderType.MARKET,
            Decimal("2"), NOW, 0,
        ),
        OrderAuthority("trader", "run", "order", NOW, 0),
        NOW, 0, ExecutionPolicy("paper"),
    )


def _market_slice() -> TimeSlice:
    return TimeSlice(
        1, NOW,
        (
            Quote(
                INSTRUMENT, EventTime(NOW, NOW, NOW),
                Decimal("99"), Decimal("101"), Decimal("10"), Decimal("10"),
            ),
        ),
    )


@pytest.mark.parametrize(
    "remaining",
    [
        1,
        1.5,
        True,
        "1",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0"),
        Decimal("-1"),
        Decimal("2.01"),
    ],
)
def test_direct_paper_execution_rejects_invalid_remaining_quantity(remaining) -> None:
    engine = SimulationExecutionEngine()
    with pytest.raises(TradingResearchError, match="remaining_quantity"):
        engine.execute(
            _approved_order(), _market_slice(), remaining_quantity=remaining,
        )
    # A rejected quote/quantity cannot poison a corrected paper-only attempt.
    corrected = engine.execute(
        _approved_order(), _market_slice(), remaining_quantity=Decimal("1"),
    )
    assert corrected.state is OrderState.FILLED
    assert corrected.fill is not None
    assert corrected.fill.quantity == Decimal("1")
    assert corrected.remaining_quantity == Decimal("0")


def test_remaining_decimal_subclass_cannot_run_custom_comparison() -> None:
    invoked: list[str] = []

    class HostileDecimal(Decimal):
        def __le__(self, other):
            invoked.append("le")
            raise AssertionError("untrusted numeric comparator")

        def __gt__(self, other):
            invoked.append("gt")
            raise AssertionError("untrusted numeric comparator")

    with pytest.raises(TradingResearchError, match="remaining_quantity"):
        SimulationExecutionEngine().execute(
            _approved_order(),
            _market_slice(),
            remaining_quantity=HostileDecimal("1"),
        )
    assert invoked == []


def test_default_quantity_remains_approved_and_nonduplicated() -> None:
    update = SimulationExecutionEngine().execute(_approved_order(), _market_slice())
    assert update.state is OrderState.FILLED
    assert update.fill is not None
    assert update.fill.quantity == Decimal("2")
    assert update.remaining_quantity == Decimal("0")


def test_explicit_partial_remaining_never_recreates_original_quantity() -> None:
    update = SimulationExecutionEngine().execute(
        _approved_order(), _market_slice(), remaining_quantity=Decimal("0.5"),
    )
    assert update.state is OrderState.FILLED
    assert update.fill is not None
    assert update.fill.quantity == Decimal("0.5")
    assert update.remaining_quantity == Decimal("0")
