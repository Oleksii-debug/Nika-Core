from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.sports_lab import (
    PaperLeg,
    PaperLegResult,
    PaperPortfolio,
    PaperQuote,
    PaperSettlement,
    PaperTicket,
    ScenarioOutcome,
    SportsLabPaperError,
    analyze_scenarios,
    enumerate_market_scenarios,
    evaluate_scenario,
)


class _HostileIdentity:
    def __str__(self) -> str:
        return "a"


class _StringSubclass(str):
    pass


def _at() -> datetime:
    return datetime(2026, 9, 12, 12, tzinfo=UTC)


def _quote(
    quote_id: str,
    event_id: str,
    market_id: str,
    selection_id: str,
    odds: str,
) -> PaperQuote:
    at = _at()
    return PaperQuote(
        quote_id=quote_id,
        source_id="paper-source",
        event_id=event_id,
        market_id=market_id,
        selection_id=selection_id,
        decimal_odds=Decimal(odds),
        observed_at=at,
        available_at=at,
    )


def test_parlay_settlement_and_bankroll_are_exact_decimal() -> None:
    ticket = PaperTicket(
        ticket_id="ticket-1",
        stake=Decimal(100),
        placed_at=_at(),
        legs=(
            PaperLeg(_quote("q1", "e1", "m1", "a", "2.00")),
            PaperLeg(_quote("q2", "e2", "m2", "b", "3.00")),
        ),
    )
    portfolio = PaperPortfolio(Decimal(1000), (ticket,))
    report = evaluate_scenario(
        portfolio,
        ScenarioOutcome("all-win", {"m1": "a", "m2": "b"}),
        settled_at=_at() + timedelta(hours=1),
    )

    assert ticket.kind == "parlay"
    assert ticket.combined_decimal_odds == Decimal("6.00")
    assert portfolio.committed_stake == Decimal(100)
    assert report.final_bankroll == Decimal("1500.00")
    assert report.profit_loss == Decimal("500.00")


def test_one_losing_leg_zeroes_parlay_payout() -> None:
    ticket = PaperTicket(
        ticket_id="ticket-1",
        stake=Decimal(100),
        placed_at=_at(),
        legs=(
            PaperLeg(_quote("q1", "e1", "m1", "a", "2.00")),
            PaperLeg(_quote("q2", "e2", "m2", "b", "3.00")),
        ),
    )
    report = evaluate_scenario(
        PaperPortfolio(Decimal(1000), (ticket,)),
        ScenarioOutcome("one-loss", {"m1": "other", "m2": "b"}),
        settled_at=_at() + timedelta(hours=1),
    )

    assert report.final_bankroll == Decimal(900)
    assert report.profit_loss == Decimal(-100)


def test_portfolio_envelope_finds_worst_and_best_case() -> None:
    ticket_a = PaperTicket(
        "ticket-a",
        Decimal(50),
        _at(),
        (PaperLeg(_quote("q1", "e1", "m1", "a", "2.00")),),
    )
    ticket_b = PaperTicket(
        "ticket-b",
        Decimal(50),
        _at(),
        (PaperLeg(_quote("q2", "e2", "m2", "b", "4.00")),),
    )
    portfolio = PaperPortfolio(Decimal(1000), (ticket_a, ticket_b))
    scenarios = enumerate_market_scenarios(
        {"m1": ("a", "other-a"), "m2": ("b", "other-b")}
    )

    envelope = analyze_scenarios(
        portfolio,
        scenarios,
        settled_at=_at() + timedelta(hours=1),
    )

    assert len(envelope.reports) == 4
    assert envelope.worst_case_profit_loss == Decimal(-100)
    assert envelope.best_case_profit_loss == Decimal(200)


def test_future_quote_cannot_be_used_in_paper_ticket() -> None:
    quote = PaperQuote(
        "q",
        "source",
        "event",
        "market",
        "selection",
        Decimal("2.00"),
        _at(),
        _at() + timedelta(seconds=30),
    )

    with pytest.raises(SportsLabPaperError, match="availability"):
        PaperTicket(
            "ticket",
            Decimal(10),
            _at() + timedelta(seconds=10),
            (PaperLeg(quote),),
        )


@pytest.mark.parametrize("value", [2.1, True, False])
def test_non_exact_numeric_quote_input_fails_closed(value: object) -> None:
    with pytest.raises(SportsLabPaperError, match="exact decimal"):
        PaperQuote(
            "q",
            "source",
            "event",
            "market",
            "selection",
            value,  # type: ignore[arg-type]
            _at(),
            _at(),
        )


def test_exact_enumeration_fails_closed_before_combinatorial_explosion() -> None:
    with pytest.raises(SportsLabPaperError, match="safety limit"):
        enumerate_market_scenarios(
            {
                "m1": ("a", "b"),
                "m2": ("a", "b"),
                "m3": ("a", "b"),
                "m4": ("a", "b"),
                "m5": ("a", "b"),
            },
            max_scenarios=16,
        )


def test_virtual_bankroll_cannot_be_overcommitted() -> None:
    ticket = PaperTicket(
        "ticket",
        Decimal(101),
        _at(),
        (PaperLeg(_quote("q", "e", "m", "s", "2.00")),),
    )

    with pytest.raises(SportsLabPaperError, match="virtual bankroll"):
        PaperPortfolio(Decimal(100), (ticket,))


def test_hostile_string_coercion_cannot_manufacture_scenario_winner() -> None:
    ticket = PaperTicket(
        "ticket",
        Decimal(100),
        _at(),
        (PaperLeg(_quote("q", "e", "m1", "a", "2.00")),),
    )
    portfolio = PaperPortfolio(Decimal(1000), (ticket,))

    with pytest.raises(SportsLabPaperError, match="exact string"):
        ScenarioOutcome(
            "hostile",
            {"m1": _HostileIdentity()},  # type: ignore[dict-item]
        )

    valid_report = evaluate_scenario(
        portfolio,
        ScenarioOutcome("valid", {"m1": "a"}),
        settled_at=_at() + timedelta(hours=1),
    )
    assert valid_report.profit_loss == Decimal(100)


def test_non_string_market_key_fails_closed_before_enumeration_sort() -> None:
    outcomes: dict[object, tuple[str, ...]] = {1: ("a",), "m2": ("b",)}
    with pytest.raises(SportsLabPaperError, match="exact string"):
        enumerate_market_scenarios(outcomes)  # type: ignore[arg-type]


def test_string_subclasses_fail_closed_across_identity_boundaries() -> None:
    subclass = _StringSubclass("identity")

    with pytest.raises(SportsLabPaperError, match="exact string"):
        PaperQuote(
            subclass,
            "source",
            "event",
            "market",
            "selection",
            Decimal("2.00"),
            _at(),
            _at(),
        )
    with pytest.raises(SportsLabPaperError, match="exact string"):
        PaperTicket(subclass, Decimal(10), _at(), ())
    with pytest.raises(SportsLabPaperError, match="exact string"):
        PaperSettlement(subclass, "selection", PaperLegResult.WIN, _at())
    with pytest.raises(SportsLabPaperError, match="exact string"):
        ScenarioOutcome(subclass, {"market": "selection"})
    with pytest.raises(SportsLabPaperError, match="exact string"):
        enumerate_market_scenarios({"market": (subclass,)})


def test_normalized_market_identity_collisions_fail_closed() -> None:
    with pytest.raises(SportsLabPaperError, match="collide"):
        ScenarioOutcome(
            "collision",
            {"market": "a", " market ": "b"},
        )

    with pytest.raises(SportsLabPaperError, match="collide"):
        enumerate_market_scenarios(
            {"market": ("a",), " market ": ("b",)}
        )


def test_identity_controls_and_oversized_values_fail_closed() -> None:
    with pytest.raises(SportsLabPaperError, match="control characters"):
        ScenarioOutcome("bad\nscenario", {"market": "selection"})

    with pytest.raises(SportsLabPaperError, match="size limit"):
        ScenarioOutcome("x" * 257, {"market": "selection"})
