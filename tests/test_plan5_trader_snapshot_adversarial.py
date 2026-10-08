"""Plan 5 Trader: fail closed on forged snapshots and pending cost policies."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research.accounting import AccountSnapshot, Position
from nika_core.trading_research.contracts import Instrument, TradingResearchError, Venue
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    Side,
)
from nika_core.trading_research.risk import (
    PendingRiskOrder,
    RiskEngine,
    RiskLimits,
    RiskState,
)

NOW = datetime(2026, 8, 26, tzinfo=UTC)
INSTRUMENT = Instrument("PLAN5-SNAPSHOT", Venue("PAPER", "UTC"), "USD")


def _snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        cash=Decimal(1000),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )


def _intent(identifier: str) -> OrderIntent:
    return OrderIntent(
        identifier, INSTRUMENT, Side.BUY, OrderType.MARKET,
        Decimal(1), NOW, 0,
    )


def _authority(identifier: str) -> OrderAuthority:
    return OrderAuthority("plan5-workspace", "paper-run", identifier, NOW, 0)


def _engine() -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_abs_position=Decimal(100),
        max_gross_exposure=Decimal(10000),
        max_net_exposure=Decimal(10000),
        max_session_loss=Decimal(10000),
        max_drawdown=Decimal(10000),
        max_leverage=Decimal(10),
    ))


def _approve(*, snapshot=None, pending=(), policy=None):
    return _engine().approve(
        _intent("candidate"),
        authority=_authority("candidate"),
        snapshot=_snapshot() if snapshot is None else snapshot,
        mark_price=Decimal(100),
        pending_signed_quantity=Decimal(0),
        approved_at=NOW,
        approved_slice=0,
        policy=policy or ExecutionPolicy("candidate-policy"),
        risk_state=RiskState(Decimal(1000), Decimal(1000)),
        pending_orders=pending,
    )


@pytest.mark.parametrize("changes", (
    {"fees": Decimal(-1)},
    {"gross_exposure": Decimal(-1)},
    {"gross_exposure": Decimal(0), "net_exposure": Decimal(1)},
    {"gross_exposure": Decimal(1), "net_exposure": Decimal(-2)},
))
def test_impossible_account_aggregates_do_not_grant_risk(changes):
    with pytest.raises(TradingResearchError):
        _approve(snapshot=replace(_snapshot(), **changes))
    with pytest.raises(TradingResearchError):
        _engine().assert_post_fill(
            replace(_snapshot(), **changes),
            RiskState(Decimal(1000), Decimal(1000)),
        )


def test_duplicate_position_identity_cannot_hide_exposure():
    position = Position(INSTRUMENT, quantity=Decimal(1), average_price=Decimal(100))
    forged = replace(
        _snapshot(),
        positions=(position, position),
        gross_exposure=Decimal(200),
        net_exposure=Decimal(200),
    )
    with pytest.raises(TradingResearchError, match="duplicate instrument"):
        _approve(snapshot=forged)


def test_malformed_position_collection_and_row_fail_closed():
    with pytest.raises(TradingResearchError):
        _approve(snapshot=replace(_snapshot(), positions=[object()]))
    with pytest.raises(TradingResearchError):
        _approve(snapshot=replace(_snapshot(), positions=(object(),)))


@pytest.mark.parametrize("bad", (
    Decimal("-1000"), Decimal("Infinity"), Decimal("NaN"),
))
def test_mutated_pending_policy_cannot_reduce_cash_reservation(bad):
    pending_policy = ExecutionPolicy("pending-policy", fixed_fee=Decimal(1))
    approved = RiskApprovedOrder(
        "approved", _intent("pending"), _authority("pending"),
        NOW, 0, pending_policy,
    )
    pending = PendingRiskOrder(approved, Decimal(100))
    object.__setattr__(pending_policy, "fixed_fee", bad)
    with pytest.raises(TradingResearchError):
        _approve(pending=(pending,))


def test_mutated_candidate_policy_is_revalidated_at_risk_admission():
    policy = ExecutionPolicy("candidate-policy")
    object.__setattr__(policy, "fee_bps", Decimal(-1))
    with pytest.raises(TradingResearchError):
        _approve(policy=policy)


def test_positive_snapshot_and_costed_pending_order_remain_supported():
    policy = ExecutionPolicy("valid-pending", fixed_fee=Decimal(1))
    approved = RiskApprovedOrder(
        "approved", _intent("pending"), _authority("pending"), NOW, 0, policy,
    )
    accepted = _approve(
        pending=(PendingRiskOrder(approved, Decimal(100)),),
    )
    assert accepted.authority.workspace_id == "plan5-workspace"
