from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from nika_core.trading_research.contracts import CausalityViolation, Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutProtocol,
    PartitionResult,
    PartitionWindow,
    RefitPolicy,
    ReplayDataQuality,
    StrategyArtifactFingerprint,
    bind_held_out_test,
    select_validation_candidate,
)
from nika_core.trading_research.metrics import (
    EquityPoint,
    RatioUnavailableReason,
    SamplingMode,
    SamplingSpec,
    calculate_performance,
    max_drawdown,
    returns_from_equity,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64
UNIVERSE = "b" * 64
METRIC = "c" * 64
QUALITY = "d" * 64
ALGORITHM = "e" * 64
CONFIG = "f" * 64
FEATURES = "1" * 64
FITTED = "2" * 64
CLEAN = ReplayDataQuality(0, 0, 0, QUALITY)


def regular_sampling(periods_per_year: str = "1") -> SamplingSpec:
    return SamplingSpec(
        SamplingMode.REGULAR,
        "continuous-utc-daily-v1",
        timedelta(days=1),
        Decimal(periods_per_year),
    )


def equity(days: int, amount: str, *, tz=UTC) -> EquityPoint:
    instant = BASE + timedelta(days=days)
    return EquityPoint(instant.astimezone(tz), Decimal(amount))


def protocol(
    *,
    refit_policy: RefitPolicy = RefitPolicy.NO_REFIT,
) -> HeldOutProtocol:
    return HeldOutProtocol(
        PartitionWindow(Partition.TRAIN, BASE, BASE + timedelta(days=10)),
        PartitionWindow(
            Partition.VALIDATION,
            BASE + timedelta(days=10),
            BASE + timedelta(days=15),
        ),
        PartitionWindow(
            Partition.TEST,
            BASE + timedelta(days=16),
            BASE + timedelta(days=20),
        ),
        refit_policy,
    )


def artifact(
    strategy_id: str = "chosen",
    *,
    fitted_state: str = FITTED,
    fit_day: int = 9,
    created_day: int = 9,
    version: str = "v1",
    config: str = CONFIG,
) -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        strategy_id,
        version,
        ALGORITHM,
        config,
        FEATURES,
        fitted_state,
        7,
        BASE + timedelta(days=fit_day),
        BASE + timedelta(days=created_day),
    )


def score(
    strategy_id: str,
    value: str | None,
    *,
    partition: Partition = Partition.VALIDATION,
    metric_fingerprint: str = METRIC,
    data_quality: ReplayDataQuality = CLEAN,
    universe: str = UNIVERSE,
    universe_cutoff_at: datetime | None = None,
    strategy_artifact: StrategyArtifactFingerprint | None = None,
) -> CandidateScore:
    return CandidateScore(
        strategy_artifact or artifact(strategy_id),
        partition,
        "sharpe",
        metric_fingerprint,
        None if value is None else Decimal(value),
        HASH,
        data_quality,
        universe,
        universe_cutoff_at or BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def result_for(
    selection,
    *,
    metric_value: str | None = "0.5",
    strategy_artifact: StrategyArtifactFingerprint | None = None,
    metric_fingerprint: str | None = None,
    data_quality: ReplayDataQuality = CLEAN,
    universe: str | None = None,
    universe_cutoff_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> PartitionResult:
    p = protocol()
    return PartitionResult(
        strategy_artifact or selection.strategy_artifact,
        Partition.TEST,
        selection.metric_name,
        metric_fingerprint or selection.metric_fingerprint,
        None if metric_value is None else Decimal(metric_value),
        selection.dataset_semantic_hash,
        data_quality,
        universe or selection.universe_fingerprint,
        universe_cutoff_at or selection.universe_cutoff_at,
        evaluated_at or p.test.end_at,
    )


def test_exact_returns_drawdown_total_return_and_wipeout() -> None:
    points = (equity(0, "100"), equity(1, "110"), equity(2, "99"))
    assert returns_from_equity(points) == (Decimal("0.1"), Decimal("-0.1"))
    assert max_drawdown(points) == Decimal("0.1")
    metrics = calculate_performance(
        points,
        trade_count=2,
        sampling=regular_sampling(),
    )
    assert metrics.total_return == Decimal("-0.01")
    assert metrics.max_drawdown == Decimal("0.1")

    wipeout = (equity(0, "100"), equity(1, "0"))
    assert max_drawdown(wipeout) == Decimal(1)
    with pytest.raises(TradingResearchError, match="recover"):
        returns_from_equity((equity(0, "100"), equity(1, "0"), equity(2, "1")))


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
        sampling=regular_sampling(),
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
        sampling=regular_sampling(),
    )
    assert sortino.sortino_ratio == Decimal("1.5")


def test_metric_result_records_sampling_and_rate_assumptions() -> None:
    sampling = regular_sampling("252")
    metrics = calculate_performance(
        (equity(0, "100"), equity(1, "101"), equity(2, "99")),
        trade_count=2,
        sampling=sampling,
        risk_free_rate_per_period=Decimal("0.001"),
        minimum_acceptable_return_per_period=Decimal("0.002"),
    )
    assert metrics.sampling_fingerprint == sampling.fingerprint
    assert metrics.sampling_mode is SamplingMode.REGULAR
    assert metrics.calendar_id == "continuous-utc-daily-v1"
    assert metrics.cadence_seconds == Decimal(86400)
    assert metrics.periods_per_year == Decimal(252)
    assert metrics.risk_free_rate_per_period == Decimal("0.001")
    assert metrics.minimum_acceptable_return_per_period == Decimal("0.002")


def test_no_trade_zero_volatility_and_no_downside_are_typed() -> None:
    no_trade = calculate_performance(
        (equity(0, "100"), equity(1, "100")),
        trade_count=0,
        sampling=regular_sampling("252"),
    )
    assert no_trade.no_trade
    assert no_trade.sharpe_ratio is None
    assert no_trade.sortino_ratio is None
    assert no_trade.sharpe_unavailable_reason is RatioUnavailableReason.NO_TRADES
    assert no_trade.sortino_unavailable_reason is RatioUnavailableReason.NO_TRADES

    zero_vol = calculate_performance(
        (equity(0, "100"), equity(1, "101"), equity(2, "102.01")),
        trade_count=2,
        sampling=regular_sampling(),
    )
    assert zero_vol.sharpe_ratio is None
    assert zero_vol.sharpe_unavailable_reason is RatioUnavailableReason.ZERO_VOLATILITY
    assert zero_vol.sortino_ratio is None
    assert zero_vol.sortino_unavailable_reason is RatioUnavailableReason.NO_DOWNSIDE


def test_irregular_sampling_never_silently_annualizes_ratios() -> None:
    points = (
        equity(0, "100"),
        EquityPoint(BASE + timedelta(days=2), Decimal("101")),
        EquityPoint(BASE + timedelta(days=5), Decimal("99")),
    )
    metrics = calculate_performance(
        points,
        trade_count=2,
        sampling=SamplingSpec(SamplingMode.IRREGULAR, None, None, None),
    )
    assert metrics.sharpe_ratio is None
    assert metrics.sortino_ratio is None
    assert (
        metrics.sharpe_unavailable_reason
        is RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE
    )
    assert (
        metrics.sortino_unavailable_reason
        is RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE
    )


def test_regular_sampling_rejects_missing_or_irregular_periods() -> None:
    points = (equity(0, "100"), equity(1, "101"), equity(3, "99"))
    with pytest.raises(TradingResearchError, match="declared cadence"):
        calculate_performance(
            points,
            trade_count=2,
            sampling=regular_sampling("252"),
        )


def test_sampling_contract_and_metric_values_reject_malformed_inputs() -> None:
    with pytest.raises(TradingResearchError, match="SamplingMode"):
        SamplingSpec("regular", "daily", timedelta(days=1), Decimal(252))  # type: ignore[arg-type]
    with pytest.raises(TradingResearchError, match="finite positive Decimal"):
        SamplingSpec(
            SamplingMode.REGULAR,
            "daily",
            timedelta(days=1),
            Decimal("NaN"),
        )
    with pytest.raises(TradingResearchError, match="equity must be a Decimal"):
        EquityPoint(BASE, 100)  # type: ignore[arg-type]
    with pytest.raises(TradingResearchError, match="datetime"):
        EquityPoint("2026-01-01", Decimal(100))  # type: ignore[arg-type]
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        calculate_performance(
            (equity(0, "100"),),
            trade_count=0,
            sampling=regular_sampling(),
            risk_free_rate_per_period=1,  # type: ignore[arg-type]
        )
    with pytest.raises(TradingResearchError, match="non-negative integer"):
        calculate_performance(
            (equity(0, "100"),),
            trade_count=True,  # type: ignore[arg-type]
            sampling=regular_sampling(),
        )


def test_sampling_evidence_mutation_fails_closed() -> None:
    sampling = regular_sampling("252")
    object.__setattr__(sampling, "periods_per_year", Decimal(365))
    with pytest.raises(TradingResearchError, match="sampling evidence changed"):
        calculate_performance(
            (equity(0, "100"), equity(1, "101"), equity(2, "99")),
            trade_count=2,
            sampling=sampling,
        )


def test_timestamp_order_normalizes_timezones_and_rejects_duplicate_instants() -> None:
    kyiv = timezone(timedelta(hours=2))
    duplicate = EquityPoint(BASE.astimezone(kyiv), Decimal(101))
    with pytest.raises(TradingResearchError, match="strictly increasing"):
        returns_from_equity((equity(0, "100"), duplicate))
    with pytest.raises(TradingResearchError, match="strictly increasing"):
        returns_from_equity((equity(1, "100"), equity(0, "101")))


def test_metric_context_is_deterministic_under_hostile_global_precision() -> None:
    points = (
        equity(0, "100"),
        equity(1, "101"),
        equity(2, "103.02"),
        equity(3, "106.1106"),
    )
    baseline = calculate_performance(
        points,
        trade_count=3,
        sampling=regular_sampling(),
    )
    with localcontext() as context:
        context.prec = 4
        hostile = calculate_performance(
            points,
            trade_count=3,
            sampling=regular_sampling(),
        )
    assert hostile == baseline


def test_protocol_rejects_overlap_and_malformed_refit_policy() -> None:
    with pytest.raises(CausalityViolation, match="overlap"):
        HeldOutProtocol(
            PartitionWindow(Partition.TRAIN, BASE, BASE + timedelta(days=11)),
            PartitionWindow(
                Partition.VALIDATION,
                BASE + timedelta(days=10),
                BASE + timedelta(days=15),
            ),
            PartitionWindow(
                Partition.TEST,
                BASE + timedelta(days=16),
                BASE + timedelta(days=20),
            ),
        )
    with pytest.raises(TradingResearchError, match="RefitPolicy"):
        HeldOutProtocol(
            protocol().train,
            protocol().validation,
            protocol().test,
            "no_refit",  # type: ignore[arg-type]
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


def test_selection_binds_exact_dataset_metric_universe_and_quality_identity() -> None:
    p = protocol()
    with pytest.raises(TradingResearchError, match="metric definition"):
        select_validation_candidate(
            p,
            (
                score("a", "1"),
                score("b", "2", metric_fingerprint="9" * 64),
            ),
            selected_at=p.validation.end_at,
        )

    alternate_quality = ReplayDataQuality(0, 0, 0, "8" * 64)
    with pytest.raises(TradingResearchError, match="exact dataset quality"):
        select_validation_candidate(
            p,
            (
                score("a", "1"),
                score("b", "2", data_quality=alternate_quality),
            ),
            selected_at=p.validation.end_at,
        )

    with pytest.raises(TradingResearchError, match="fixed universe"):
        select_validation_candidate(
            p,
            (score("a", "1"), score("b", "2", universe="7" * 64)),
            selected_at=p.validation.end_at,
        )


def test_selection_rejects_missing_metric_future_fit_and_survivorship() -> None:
    p = protocol()
    with pytest.raises(TradingResearchError, match="missing"):
        select_validation_candidate(
            p,
            (score("none", None),),
            selected_at=p.validation.end_at,
        )

    future_fit = artifact("fit", fit_day=11, created_day=11)
    with pytest.raises(CausalityViolation, match="fit"):
        select_validation_candidate(
            p,
            (score("fit", "1", strategy_artifact=future_fit),),
            selected_at=p.validation.end_at,
        )

    with pytest.raises(CausalityViolation, match="universe"):
        select_validation_candidate(
            p,
            (
                score(
                    "universe",
                    "1",
                    universe_cutoff_at=p.validation.start_at,
                ),
            ),
            selected_at=p.validation.end_at,
        )


def test_heldout_no_refit_requires_exact_selected_artifact() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("chosen", "1"),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    assert assessment.require_promotion_metric() == Decimal("0.5")

    changed_state = artifact("chosen", fitted_state="3" * 64)
    with pytest.raises(CausalityViolation, match="NO_REFIT"):
        bind_held_out_test(
            p,
            selected,
            result_for(selected, strategy_artifact=changed_state),
        )


def test_refit_policy_seals_train_validation_refit_without_test_leakage() -> None:
    p = protocol(refit_policy=RefitPolicy.REFIT_TRAIN_VALIDATION)
    selected = select_validation_candidate(
        p,
        (score("chosen", "1"),),
        selected_at=p.validation.end_at,
    )
    refit = StrategyArtifactFingerprint(
        "chosen",
        "v1",
        ALGORITHM,
        CONFIG,
        FEATURES,
        "4" * 64,
        7,
        p.validation.end_at,
        p.validation.end_at + timedelta(hours=1),
    )
    result = PartitionResult(
        refit,
        Partition.TEST,
        selected.metric_name,
        selected.metric_fingerprint,
        Decimal("0.5"),
        selected.dataset_semantic_hash,
        CLEAN,
        selected.universe_fingerprint,
        selected.universe_cutoff_at,
        p.test.end_at,
    )
    assert bind_held_out_test(p, selected, result).require_promotion_metric() == Decimal("0.5")

    future_refit = StrategyArtifactFingerprint(
        "chosen",
        "v1",
        ALGORITHM,
        CONFIG,
        FEATURES,
        "5" * 64,
        7,
        p.test.start_at,
        p.test.start_at,
    )
    with pytest.raises(CausalityViolation, match="test/future"):
        bind_held_out_test(
            p,
            selected,
            PartitionResult(
                future_refit,
                Partition.TEST,
                selected.metric_name,
                selected.metric_fingerprint,
                Decimal("0.5"),
                selected.dataset_semantic_hash,
                CLEAN,
                selected.universe_fingerprint,
                selected.universe_cutoff_at,
                p.test.end_at,
            ),
        )


def test_heldout_rejects_metric_quality_universe_and_future_evidence_changes() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("chosen", "1"),),
        selected_at=p.validation.end_at,
    )
    with pytest.raises(TradingResearchError, match="metric definition"):
        bind_held_out_test(
            p,
            selected,
            result_for(selected, metric_fingerprint="9" * 64),
        )
    with pytest.raises(TradingResearchError, match="exact dataset quality"):
        bind_held_out_test(
            p,
            selected,
            result_for(
                selected,
                data_quality=ReplayDataQuality(0, 0, 0, "8" * 64),
            ),
        )
    with pytest.raises(CausalityViolation, match="fixed validation universe"):
        bind_held_out_test(
            p,
            selected,
            result_for(selected, universe="7" * 64),
        )
    with pytest.raises(CausalityViolation, match="before the test window ended"):
        bind_held_out_test(
            p,
            selected,
            result_for(
                selected,
                evaluated_at=p.test.end_at - timedelta(seconds=1),
            ),
        )


def test_unavailable_heldout_metric_cannot_promote() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("chosen", "1"),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(
        p,
        selected,
        result_for(selected, metric_value=None),
    )
    with pytest.raises(TradingResearchError, match="promotion"):
        assessment.require_promotion_metric()


def test_nonfinite_candidate_and_result_metrics_are_rejected() -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        score("nan", "NaN")
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score("chosen", "1"),),
        selected_at=p.validation.end_at,
    )
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        PartitionResult(
            selected.strategy_artifact,
            Partition.TEST,
            selected.metric_name,
            selected.metric_fingerprint,
            Decimal("Infinity"),
            HASH,
            CLEAN,
            UNIVERSE,
            selected.universe_cutoff_at,
            p.test.end_at,
        )
