from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from itertools import product
from types import MappingProxyType


class SportsLabPaperError(ValueError):
    """Fail-closed error for paper-only sports portfolio simulation."""


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SportsLabPaperError(f"{field_name} must not be empty")
    return normalized


def _decimal(
    value: Decimal | int | str,
    field_name: str,
    *,
    positive: bool = False,
    minimum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise SportsLabPaperError(f"{field_name} must use exact decimal input")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SportsLabPaperError(f"{field_name} must be an exact decimal") from exc
    if not result.is_finite():
        raise SportsLabPaperError(f"{field_name} must be finite")
    if positive and result <= 0:
        raise SportsLabPaperError(f"{field_name} must be positive")
    if minimum is not None and result < minimum:
        raise SportsLabPaperError(f"{field_name} must be at least {minimum}")
    return result


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SportsLabPaperError(f"{field_name} must be timezone-aware")
    return value


class PaperLegResult(str, Enum):
    WIN = "win"
    LOSS = "loss"
    VOID = "void"


@dataclass(frozen=True, slots=True)
class PaperQuote:
    quote_id: str
    source_id: str
    event_id: str
    market_id: str
    selection_id: str
    decimal_odds: Decimal
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("quote_id", "source_id", "event_id", "market_id", "selection_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        odds = _decimal(self.decimal_odds, "decimal_odds")
        if odds <= 1:
            raise SportsLabPaperError("decimal_odds must be greater than 1")
        observed_at = _aware(self.observed_at, "observed_at")
        available_at = _aware(self.available_at, "available_at")
        if available_at < observed_at:
            raise SportsLabPaperError("available_at must not precede observed_at")
        object.__setattr__(self, "decimal_odds", odds)


@dataclass(frozen=True, slots=True)
class PaperLeg:
    quote: PaperQuote

    @property
    def leg_key(self) -> str:
        return f"{self.quote.market_id}:{self.quote.selection_id}"


@dataclass(frozen=True, slots=True)
class PaperTicket:
    ticket_id: str
    stake: Decimal
    placed_at: datetime
    legs: tuple[PaperLeg, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticket_id", _required(self.ticket_id, "ticket_id"))
        object.__setattr__(self, "stake", _decimal(self.stake, "stake", positive=True))
        placed_at = _aware(self.placed_at, "placed_at")
        object.__setattr__(self, "placed_at", placed_at)
        legs = tuple(self.legs)
        if not legs:
            raise SportsLabPaperError("paper ticket must contain at least one leg")
        quote_ids = [leg.quote.quote_id for leg in legs]
        if len(set(quote_ids)) != len(quote_ids):
            raise SportsLabPaperError("paper ticket quote identities must be unique")
        market_ids = [leg.quote.market_id for leg in legs]
        if len(set(market_ids)) != len(market_ids):
            raise SportsLabPaperError("paper ticket cannot contain two selections from one market")
        for leg in legs:
            if leg.quote.available_at > placed_at:
                raise SportsLabPaperError(
                    "paper ticket cannot use a quote before its availability boundary"
                )
        object.__setattr__(self, "legs", legs)

    @property
    def kind(self) -> str:
        return "single" if len(self.legs) == 1 else "parlay"

    @property
    def combined_decimal_odds(self) -> Decimal:
        value = Decimal("1")
        for leg in self.legs:
            value *= leg.quote.decimal_odds
        return value


@dataclass(frozen=True, slots=True)
class PaperSettlement:
    market_id: str
    selection_id: str
    result: PaperLegResult
    settled_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", _required(self.market_id, "market_id"))
        object.__setattr__(self, "selection_id", _required(self.selection_id, "selection_id"))
        if not isinstance(self.result, PaperLegResult):
            try:
                object.__setattr__(self, "result", PaperLegResult(self.result))
            except ValueError as exc:
                raise SportsLabPaperError("unknown paper leg result") from exc
        object.__setattr__(self, "settled_at", _aware(self.settled_at, "settled_at"))

    @property
    def leg_key(self) -> str:
        return f"{self.market_id}:{self.selection_id}"


@dataclass(frozen=True, slots=True)
class TicketSettlement:
    ticket_id: str
    stake: Decimal
    payout: Decimal
    profit_loss: Decimal
    settled_at: datetime


def settle_ticket(
    ticket: PaperTicket,
    settlements: Mapping[str, PaperSettlement],
) -> TicketSettlement:
    payout_multiplier = Decimal("1")
    settled_times: list[datetime] = []
    for leg in ticket.legs:
        settlement = settlements.get(leg.leg_key)
        if settlement is None:
            raise SportsLabPaperError(f"missing settlement for leg: {leg.leg_key}")
        if settlement.market_id != leg.quote.market_id:
            raise SportsLabPaperError("settlement market identity mismatch")
        if settlement.selection_id != leg.quote.selection_id:
            raise SportsLabPaperError("settlement selection identity mismatch")
        if settlement.settled_at < ticket.placed_at:
            raise SportsLabPaperError("settlement must not precede paper placement")
        settled_times.append(settlement.settled_at)
        if settlement.result is PaperLegResult.LOSS:
            payout_multiplier = Decimal("0")
            continue
        if settlement.result is PaperLegResult.WIN:
            payout_multiplier *= leg.quote.decimal_odds
    payout = ticket.stake * payout_multiplier
    return TicketSettlement(
        ticket_id=ticket.ticket_id,
        stake=ticket.stake,
        payout=payout,
        profit_loss=payout - ticket.stake,
        settled_at=max(settled_times),
    )


@dataclass(frozen=True, slots=True)
class PaperPortfolio:
    initial_bankroll: Decimal
    tickets: tuple[PaperTicket, ...]

    def __post_init__(self) -> None:
        bankroll = _decimal(self.initial_bankroll, "initial_bankroll", minimum=Decimal("0"))
        tickets = tuple(self.tickets)
        ids = [ticket.ticket_id for ticket in tickets]
        if len(set(ids)) != len(ids):
            raise SportsLabPaperError("paper ticket identities must be unique")
        committed = sum((ticket.stake for ticket in tickets), Decimal("0"))
        if committed > bankroll:
            raise SportsLabPaperError("paper stakes exceed virtual bankroll")
        object.__setattr__(self, "initial_bankroll", bankroll)
        object.__setattr__(self, "tickets", tickets)

    @property
    def committed_stake(self) -> Decimal:
        return sum((ticket.stake for ticket in self.tickets), Decimal("0"))

    @property
    def cash_after_stakes(self) -> Decimal:
        return self.initial_bankroll - self.committed_stake

    def settle(
        self,
        settlements: Mapping[str, PaperSettlement],
    ) -> "PaperPortfolioResult":
        ticket_results = tuple(settle_ticket(ticket, settlements) for ticket in self.tickets)
        total_payout = sum((result.payout for result in ticket_results), Decimal("0"))
        final_bankroll = self.cash_after_stakes + total_payout
        return PaperPortfolioResult(
            initial_bankroll=self.initial_bankroll,
            committed_stake=self.committed_stake,
            total_payout=total_payout,
            final_bankroll=final_bankroll,
            profit_loss=final_bankroll - self.initial_bankroll,
            ticket_results=ticket_results,
        )


@dataclass(frozen=True, slots=True)
class PaperPortfolioResult:
    initial_bankroll: Decimal
    committed_stake: Decimal
    total_payout: Decimal
    final_bankroll: Decimal
    profit_loss: Decimal
    ticket_results: tuple[TicketSettlement, ...]


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    scenario_id: str
    winners_by_market: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _required(self.scenario_id, "scenario_id"))
        normalized: dict[str, str] = {}
        for market_id, selection_id in self.winners_by_market.items():
            market = _required(str(market_id), "market_id")
            selection = _required(str(selection_id), "selection_id")
            normalized[market] = selection
        object.__setattr__(self, "winners_by_market", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    scenario_id: str
    profit_loss: Decimal
    final_bankroll: Decimal


@dataclass(frozen=True, slots=True)
class ScenarioEnvelope:
    reports: tuple[ScenarioReport, ...]
    worst_case_profit_loss: Decimal
    best_case_profit_loss: Decimal
    worst_case_scenario_id: str
    best_case_scenario_id: str


def evaluate_scenario(
    portfolio: PaperPortfolio,
    scenario: ScenarioOutcome,
    *,
    settled_at: datetime,
) -> ScenarioReport:
    settled_at = _aware(settled_at, "settled_at")
    settlements: dict[str, PaperSettlement] = {}
    for ticket in portfolio.tickets:
        for leg in ticket.legs:
            winner = scenario.winners_by_market.get(leg.quote.market_id)
            if winner is None:
                raise SportsLabPaperError(
                    f"scenario omits market required by portfolio: {leg.quote.market_id}"
                )
            result = (
                PaperLegResult.WIN
                if winner == leg.quote.selection_id
                else PaperLegResult.LOSS
            )
            settlement = PaperSettlement(
                market_id=leg.quote.market_id,
                selection_id=leg.quote.selection_id,
                result=result,
                settled_at=settled_at,
            )
            existing = settlements.get(settlement.leg_key)
            if existing is not None and existing != settlement:
                raise SportsLabPaperError("scenario produced conflicting leg settlement")
            settlements[settlement.leg_key] = settlement
    result = portfolio.settle(settlements)
    return ScenarioReport(
        scenario_id=scenario.scenario_id,
        profit_loss=result.profit_loss,
        final_bankroll=result.final_bankroll,
    )


def analyze_scenarios(
    portfolio: PaperPortfolio,
    scenarios: Iterable[ScenarioOutcome],
    *,
    settled_at: datetime,
) -> ScenarioEnvelope:
    reports = tuple(
        evaluate_scenario(portfolio, scenario, settled_at=settled_at)
        for scenario in scenarios
    )
    if not reports:
        raise SportsLabPaperError("at least one scenario is required")
    worst = min(reports, key=lambda report: (report.profit_loss, report.scenario_id))
    best = max(reports, key=lambda report: (report.profit_loss, report.scenario_id))
    return ScenarioEnvelope(
        reports=reports,
        worst_case_profit_loss=worst.profit_loss,
        best_case_profit_loss=best.profit_loss,
        worst_case_scenario_id=worst.scenario_id,
        best_case_scenario_id=best.scenario_id,
    )


def enumerate_market_scenarios(
    outcomes_by_market: Mapping[str, tuple[str, ...]],
    *,
    max_scenarios: int = 4096,
) -> tuple[ScenarioOutcome, ...]:
    if type(max_scenarios) is not int or max_scenarios < 1:
        raise SportsLabPaperError("max_scenarios must be a positive integer")
    normalized: list[tuple[str, tuple[str, ...]]] = []
    scenario_count = 1
    for market_id, selections in sorted(outcomes_by_market.items()):
        market = _required(str(market_id), "market_id")
        values = tuple(_required(str(item), "selection_id") for item in selections)
        if not values:
            raise SportsLabPaperError(f"market has no outcomes: {market}")
        if len(set(values)) != len(values):
            raise SportsLabPaperError(f"market outcomes must be unique: {market}")
        scenario_count *= len(values)
        if scenario_count > max_scenarios:
            raise SportsLabPaperError(
                "exact scenario enumeration exceeds configured safety limit"
            )
        normalized.append((market, values))
    if not normalized:
        raise SportsLabPaperError("at least one market is required")
    reports: list[ScenarioOutcome] = []
    market_ids = tuple(item[0] for item in normalized)
    selection_sets = tuple(item[1] for item in normalized)
    for index, winners in enumerate(product(*selection_sets), start=1):
        reports.append(
            ScenarioOutcome(
                scenario_id=f"scenario-{index:06d}",
                winners_by_market=dict(zip(market_ids, winners, strict=True)),
            )
        )
    return tuple(reports)
