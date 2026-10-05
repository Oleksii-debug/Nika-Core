from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import Instrument, TradingResearchError
from .identity import InstrumentIdentity, instrument_identity
from .orders import Side, SimulatedFill


@dataclass(frozen=True, slots=True)
class Position:
    instrument: Instrument
    quantity: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: Decimal
    fees: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    positions: tuple[Position, ...]


@dataclass(slots=True)
class PortfolioLedger:
    starting_cash: Decimal
    _cash: Decimal = field(init=False)
    _fees: Decimal = field(default=Decimal(0), init=False)
    _positions: dict[InstrumentIdentity, Position] = field(default_factory=dict, init=False)
    _scope: tuple[str, str] | None = field(default=None, init=False)
    _applied_fills: dict[str, tuple[object, ...]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.starting_cash < 0:
            raise TradingResearchError("starting_cash cannot be negative")
        self._cash = Decimal(self.starting_cash)

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def fees(self) -> Decimal:
        return self._fees

    def position(self, instrument: Instrument) -> Position:
        return self._positions.get(instrument_identity(instrument), Position(instrument))

    def apply_fill(self, fill: SimulatedFill) -> None:
        scope = (fill.authority.workspace_id, fill.authority.run_id)
        if self._scope is None:
            self._scope = scope
        elif self._scope != scope:
            raise TradingResearchError("portfolio ledger cannot mix workspace/run scope")
        evidence = _fill_evidence(fill)
        existing = self._applied_fills.get(fill.fill_id)
        if existing is not None:
            if existing != evidence:
                raise TradingResearchError("conflicting in-memory fill identity")
            return
        current = self.position(fill.instrument)
        signed_fill = fill.quantity * Decimal(fill.side.sign)
        gross = fill.quantity * fill.price
        self._cash += (-gross if fill.side is Side.BUY else gross) - fill.fee
        self._fees += fill.fee
        self._positions[instrument_identity(fill.instrument)] = _apply_position_fill(
            current, signed_fill, fill.price
        )
        self._applied_fills[fill.fill_id] = evidence

    def snapshot(self, marks: Mapping[InstrumentIdentity, Decimal]) -> AccountSnapshot:
        positions = tuple(
            sorted(self._positions.values(), key=lambda item: instrument_identity(item.instrument))
        )
        realized = sum((item.realized_pnl for item in positions), Decimal(0))
        unrealized = Decimal(0)
        gross = Decimal(0)
        net = Decimal(0)
        market_value = Decimal(0)
        for item in positions:
            if item.quantity == 0:
                continue
            identity = instrument_identity(item.instrument)
            mark = marks.get(identity)
            if mark is None or mark <= 0:
                raise TradingResearchError(
                    f"positive mark required for open position identity {identity!r}"
                )
            value = item.quantity * mark
            market_value += value
            gross += abs(value)
            net += value
            unrealized += (mark - item.average_price) * item.quantity
        return AccountSnapshot(
            cash=self._cash,
            fees=self._fees,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            equity=self._cash + market_value,
            gross_exposure=gross,
            net_exposure=net,
            positions=positions,
        )

    def has_applied_fill(self, fill_id: str) -> bool:
        return fill_id in self._applied_fills




def _fill_evidence(fill: SimulatedFill) -> tuple[object, ...]:
    return (
        fill.authority.workspace_id,
        fill.authority.run_id,
        fill.authority.order_id,
        fill.approval_id,
        fill.intent_id,
        instrument_identity(fill.instrument),
        fill.side,
        fill.quantity,
        fill.price,
        fill.fee,
        fill.filled_at,
        fill.filled_slice,
    )


def _apply_position_fill(position: Position, signed_fill: Decimal, price: Decimal) -> Position:
    old_qty = position.quantity
    new_qty = old_qty + signed_fill
    if old_qty == 0:
        average = price if new_qty else Decimal(0)
        return Position(position.instrument, new_qty, average, position.realized_pnl)
    same_direction = (old_qty > 0 and signed_fill > 0) or (old_qty < 0 and signed_fill < 0)
    if same_direction:
        total_cost = abs(old_qty) * position.average_price + abs(signed_fill) * price
        average = total_cost / abs(new_qty)
        return Position(position.instrument, new_qty, average, position.realized_pnl)
    closing_qty = min(abs(old_qty), abs(signed_fill))
    direction = Decimal(1) if old_qty > 0 else Decimal(-1)
    realized = position.realized_pnl + (price - position.average_price) * closing_qty * direction
    if new_qty == 0:
        average = Decimal(0)
    elif (new_qty > 0) == (old_qty > 0):
        average = position.average_price
    else:
        average = price
    return Position(position.instrument, new_qty, average, realized)
