from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from nika_core.trading_research.contracts import CausalityViolation, Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutProtocol,
    PartitionResult,
    PartitionWindow,
    ReplayDataQuality,
    bind_held_out_test,
    select_validation_candidate,
)
from nika_core.trading_research.metrics import (
    EquityPoint,
    RatioUnavailableReason,
    calculate_performance,
    max_drawdown,
    returns_from_equity,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64
CLEAN = ReplayDataQuality(0, 0, 0)


def equity(minutes: int, amount: str, *, tz=UTC) -> EquityPoint:
    instant = BASE + timedelta(minutes=minutes)
    return EquityPoint(instant.astimezone(tz), Decimal(amount))


def protocol() -> HeldOutProtocol:
    return HeldOutProtocol(
        PartitionWindow(Partition.TRAIN, BASE, BASE + timedelta(days=10)),
        PartitionWindow(
            Partition.VALIDATION,
            BASE + timedelta(days=10),
            BASE + timedelta(days=15),
        ),
        PartitionWindow(
            Partition.TEST,
            BASE + timedelta(days=15),
            BASE + timedelta(days=20),
        ),
    )


def score(strategy_id: str, value: str | None, *, partition=Partition.VALIDATION):
    return CandidateScore(
        strategy_id,
        partition,
        "sharpe",
        None if value is None else Decimal(value),
        HASH,
        CLEAN,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def test_exact_returns_drawdown_and_total_return() -> None:
    points = (equity(0, "100"), equity(1, "110"), equity(2, "99"))
    assert returns_from_equity(points) == (Decimal("0.1"), Decimal("-0.1"))
    assert max_drawdown(points) == Decimal("0.1")
    metrics = calculate_performance(points, trade_count=2, periods_per_year=Decimal(1))
    assert metrics.total_return == Decimal("-0.01")
    assert metrics.max_drawdown == Decimal("0.1")


def test_sharpe_and_sortino_use_manual_decimal_oracles() -> None:
    sharpe_points = (
        equity(0, "100"),
        equity(1, "101"),
        equity(2, "103.02"),
        equity(3, "106.1106"),
    )
    sharpe = calculate_performance(
        sharpe_points,
        trade_count=3,
        periods_per_year=Decimal(1),
    )
    assert sharpe.sharpe_ratio == Decimal(2)

    sortino_points = (
        equity(0, "100"),
        equity(1, "102"),
        equity(2, "100.98"),
        equity(3, "102.9996"),
        equity(4, "102.9996"),
    )
    sortino = calculate_performance(
        sortino_points,
        trade_count=4,
        periods_per_year=Decimal(1),
    )
    assert sortino.sortino_ratio == Decimal("1.5")


def test_no_trade_and_divide_by_zero_cases_are_explicit() -> None:
    metrics = calculate_performance(
        (equity(0, "100"), equity(1, "100")),
        trade_count=0,
        periods_per_year=Decimal(252),
    )
    assert metrics.no_trade
    assert metrics.sharpe_ratio is None
    assert metrics.sortino_ratio is None
    assert metrics.sharpe_unavailable_reason is RatioUnavailableReason.NO_TRADES
    assert metrics.sortino_unavailable_reason is RatioUnavailableReason.NO_TRADES

    zero_vol = calculate_performance(
        (equity(0, "100"), equity(1, "101"), equity(2, "102.01")),
        trade_count=2,
        periods_per_year=Decimal(1),
    )
    assert zero_vol.sharpe_ratio is None
    assert zero_vol.sharpe_unavailable_reason is RatioUnavailableReason.ZERO_VOLATILITY
    assert zero_vol.sortino_ratio is None
    assert zero_vol.sortino_unavailable_reason is RatioUnavailableReason.NO_DOWNSIDE


def test_wipeout_drawdown_is_one_and_recovery_after_zero_fails_closed() -> None:
    assert max_drawdown((equity(0, "100"), equity(1, "0"))) == Decimal(1)
    with pytest.raises(TradingResearchError, match="recover"):
        returns_from_equity((equity(0, "100"), equity(1, "0"), equity(2, "1")))


def test_timestamp_order_normalizes_timezones_and_rejects_duplicate_instants() -> None:
    kyiv = timezone(timedelta(hours=2))
    duplicate_instant = EquityPoint(BASE.astimezone(kyiv), Decimal(101))
    with pytest.raises(TradingResearchError, match="strictly increasing"):
        returns_from_equity((equity(0, "100"), duplicate_instant))
    with pytest.raises(TradingResearchError, match="strictly increasing"):
        returns_from_equity((equity(1, "100"), equity(0, "101")))


def test_metric_context_is_deterministic_under_hostile_global_precision() -> None:
    points = (
        equity(0, "100"),
        equity(1, "101"),
        equity(2, "103.02"),
        equity(3, "106.1106"),
    )
    baseline = calculate_performance(points, trade_count=3, periods_per_year=Decimal(1))
    with localcontext() as context:
        context.prec = 4
        hostile = calculate_performance(points, trade_count=3, periods_per_year=Decimal(1))
    assert hostile == baseline


def test_nonfinite_values_and_invalid_annualization_fail_closed() -> None:
    with pytest.raises(TradingResearchError, match="finite"):
        EquityPoint(BASE, Decimal("NaN"))
    with pytest.raises(TradingResearchError, match="periods_per_year"):
        calculate_performance(
            (equity(0, "100"),), trade_count=0, periods_per_year=Decimal(0)
        )


def test_protocol_rejects_overlapping_partitions() -> None:
    with pytest.raises(CausalityViolation, match="overlap"):
        HeldOutProtocol(
            PartitionWindow(Partition.TRAIN, BASE, BASE + timedelta(days=11)),
            PartitionWindow(
                Partition.VALIDATION, BASE + timedelta(days=10), BASE + timedelta(days=15)
            ),
            PartitionWindow(Partition.TEST, BASE + timedelta(days=15), BASE + timedelta(days=20)),
        )


def test_selection_is_validation_only_and_tie_breaks_deterministically() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("zeta", "2"), score("alpha", "2"), score("middle", "1")),
        selected_at=p.validation.end_at,
    )
    assert selected.strategy_id == "alpha"
    with pytest.raises(CausalityViolation, match="validation"):
        select_validation_candidate(
            p,
            (score("leaky", "9", partition=Partition.TEST),),
            selected_at=p.validation.end_at,
        )


def test_selection_rejects_missing_metric_future_fit_and_survivorship_leakage() -> None:
    p = protocol()
    with pytest.raises(TradingResearchError, match="missing"):
        select_validation_candidate(p, (score("none", None),), selected_at=p.validation.end_at)
    future_fit = score("fit", "1")
    future_fit = CandidateScore(
        future_fit.strategy_id,
        future_fit.partition,
        future_fit.metric_name,
        future_fit.metric_value,
        future_fit.dataset_semantic_hash,
        CLEAN,
        p.validation.start_at + timedelta(seconds=1),
        future_fit.universe_cutoff_at,
        future_fit.evaluated_at,
    )
    with pytest.raises(CausalityViolation, match="fit"):
        select_validation_candidate(p, (future_fit,), selected_at=p.validation.end_at)
    future_universe = CandidateScore(
        "universe",
        Partition.VALIDATION,
        "sharpe",
        Decimal(1),
        HASH,
        CLEAN,
        BASE + timedelta(days=9),
        p.validation.start_at + timedelta(seconds=1),
        p.validation.end_at,
    )
    with pytest.raises(CausalityViolation, match="universe"):
        select_validation_candidate(p, (future_universe,), selected_at=p.validation.end_at)


def test_selection_must_finish_before_heldout_test_starts() -> None:
    p = protocol()
    with pytest.raises(CausalityViolation, match="after held-out"):
        select_validation_candidate(
            p,
            (score("a", "1"),),
            selected_at=p.test.start_at + timedelta(seconds=1),
        )


def test_heldout_binding_rejects_strategy_dataset_fit_and_universe_mismatch() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p, (score("chosen", "1"),), selected_at=p.validation.end_at
    )

    def result(**changes):
        values = {
            "strategy_id": "chosen",
            "partition": Partition.TEST,
            "metric_name": "sharpe",
            "metric_value": Decimal("0.5"),
            "dataset_semantic_hash": HASH,
            "data_quality": CLEAN,
            "fit_cutoff_at": p.test.start_at,
            "universe_cutoff_at": p.test.start_at,
            "evaluated_at": p.test.end_at,
        }
        values.update(changes)
        return PartitionResult(**values)

    assert bind_held_out_test(p, selected, result()).require_promotion_metric() == Decimal("0.5")
    with pytest.raises(CausalityViolation, match="selected strategy"):
        bind_held_out_test(p, selected, result(strategy_id="other"))
    with pytest.raises(TradingResearchError, match="dataset"):
        bind_held_out_test(p, selected, result(dataset_semantic_hash="b" * 64))
    with pytest.raises(CausalityViolation, match="fit"):
        bind_held_out_test(
            p, selected, result(fit_cutoff_at=p.test.start_at + timedelta(seconds=1))
        )
    with pytest.raises(CausalityViolation, match="universe"):
        bind_held_out_test(
            p,
            selected,
            result(universe_cutoff_at=p.test.start_at + timedelta(seconds=1)),
        )
    with pytest.raises(CausalityViolation, match="before the test window ended"):
        bind_held_out_test(
            p, selected, result(evaluated_at=p.test.end_at - timedelta(seconds=1))
        )


def test_dirty_data_quality_cannot_select_or_bind_heldout_evidence() -> None:
    p = protocol()
    dirty_score = CandidateScore(
        "dirty",
        Partition.VALIDATION,
        "sharpe",
        Decimal(1),
        HASH,
        ReplayDataQuality(1, 0, 1),
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
        p.validation.end_at,
    )
    with pytest.raises(TradingResearchError, match="duplicate"):
        select_validation_candidate(p, (dirty_score,), selected_at=p.validation.end_at)

    selected = select_validation_candidate(
        p, (score("chosen", "1"),), selected_at=p.validation.end_at
    )
    dirty_result = PartitionResult(
        "chosen",
        Partition.TEST,
        "sharpe",
        Decimal("0.5"),
        HASH,
        ReplayDataQuality(0, 1, 0),
        p.test.start_at,
        p.test.start_at,
        p.test.end_at,
    )
    with pytest.raises(TradingResearchError, match="duplicate"):
        bind_held_out_test(p, selected, dirty_result)


def test_metric_direction_and_temporal_evidence_are_reproducible() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("low", "0.1"), score("high", "0.2")),
        selected_at=p.validation.end_at,
        higher_is_better=False,
    )
    assert selected.strategy_id == "low"
    assert not selected.higher_is_better

    too_early = CandidateScore(
        "early",
        Partition.VALIDATION,
        "sharpe",
        Decimal(1),
        HASH,
        CLEAN,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
        p.validation.end_at - timedelta(seconds=1),
    )
    with pytest.raises(CausalityViolation, match="before validation ended"):
        select_validation_candidate(p, (too_early,), selected_at=p.validation.end_at)



def test_unavailable_heldout_metric_cannot_be_used_as_promotion_evidence() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p, (score("chosen", "1"),), selected_at=p.validation.end_at
    )
    assessment = bind_held_out_test(
        p,
        selected,
        PartitionResult(
            "chosen",
            Partition.TEST,
            "sharpe",
            None,
            HASH,
            CLEAN,
            p.test.start_at,
            p.test.start_at,
            p.test.end_at,
        ),
    )
    with pytest.raises(TradingResearchError, match="promotion"):
        assessment.require_promotion_metric()
