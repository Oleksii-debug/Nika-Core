import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research import heldout, metrics
from nika_core.trading_research.contracts import CausalityViolation, Partition, TradingResearchError
from nika_core.trading_research.dataset import ValidationIssue, ValidationReport
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
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DATASET = "a" * 64
UNIVERSE = "b" * 64
METRIC = "c" * 64
QUALITY = "d" * 64
ALGORITHM = "e" * 64
CONFIG = "f" * 64
FEATURES = "1" * 64
FITTED = "2" * 64
CLEAN = ReplayDataQuality(0, 0, 0, QUALITY)


def protocol(*, refit: RefitPolicy = RefitPolicy.NO_REFIT) -> HeldOutProtocol:
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
        refit,
    )


def artifact(
    *,
    fitted: str = FITTED,
    fit_at: datetime | None = None,
    created_at: datetime | None = None,
    config: str = CONFIG,
) -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        "candidate",
        "v1",
        ALGORITHM,
        config,
        FEATURES,
        fitted,
        17,
        fit_at or BASE + timedelta(days=9),
        created_at or BASE + timedelta(days=9),
    )


def score(*, strategy_artifact: StrategyArtifactFingerprint | None = None) -> CandidateScore:
    return CandidateScore(
        strategy_artifact or artifact(),
        Partition.VALIDATION,
        "sharpe",
        METRIC,
        Decimal("1.25"),
        DATASET,
        CLEAN,
        UNIVERSE,
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def selected(p: HeldOutProtocol):
    return select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )


def result_for(
    p: HeldOutProtocol,
    selection,
    *,
    strategy_artifact: StrategyArtifactFingerprint | None = None,
    metric_fingerprint: str = METRIC,
    quality: ReplayDataQuality = CLEAN,
) -> PartitionResult:
    return PartitionResult(
        strategy_artifact or selection.strategy_artifact,
        Partition.TEST,
        "sharpe",
        metric_fingerprint,
        Decimal("0.75"),
        DATASET,
        quality,
        UNIVERSE,
        selection.universe_cutoff_at,
        p.test.end_at,
    )


def test_aud03_irregular_grid_cannot_claim_annualized_sharpe_or_sortino() -> None:
    points = (
        EquityPoint(BASE, Decimal(100)),
        EquityPoint(BASE + timedelta(days=2), Decimal(101)),
        EquityPoint(BASE + timedelta(days=5), Decimal(99)),
    )
    result = calculate_performance(
        points,
        trade_count=2,
        sampling=SamplingSpec(SamplingMode.IRREGULAR, None, None, None),
    )
    assert result.sharpe_ratio is None
    assert result.sortino_ratio is None
    assert result.sharpe_unavailable_reason is RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE
    assert result.sortino_unavailable_reason is RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE


def test_aud03_regular_grid_rejects_missing_period_instead_of_reannualizing() -> None:
    sampling = SamplingSpec(
        SamplingMode.REGULAR,
        "aud03-daily-v1",
        timedelta(days=1),
        Decimal(252),
    )
    points = (
        EquityPoint(BASE, Decimal(100)),
        EquityPoint(BASE + timedelta(days=1), Decimal(101)),
        EquityPoint(BASE + timedelta(days=3), Decimal(102)),
    )
    with pytest.raises(TradingResearchError, match="declared cadence"):
        calculate_performance(points, trade_count=2, sampling=sampling)


def test_aud03_same_metric_name_with_changed_definition_fails_closed() -> None:
    p = protocol()
    selection = selected(p)
    with pytest.raises(TradingResearchError, match="metric definition"):
        bind_held_out_test(
            p,
            selection,
            result_for(p, selection, metric_fingerprint="9" * 64),
        )


def test_aud03_same_quality_counts_with_different_evidence_fails_closed() -> None:
    p = protocol()
    selection = selected(p)
    report = ValidationReport(
        duplicates=(ValidationIssue("duplicate", "aud03 adversarial issue", (1, 2)),),
    )
    dirty = ReplayDataQuality.from_report(report)
    assert dirty.duplicate_count == 1
    with pytest.raises(TradingResearchError, match="duplicate"):
        bind_held_out_test(p, selection, result_for(p, selection, quality=dirty))


def test_aud03_same_strategy_id_cannot_swap_config_at_heldout_boundary() -> None:
    p = protocol()
    selection = selected(p)
    changed = artifact(config="9" * 64)
    with pytest.raises(CausalityViolation, match="strategy definition"):
        bind_held_out_test(
            p,
            selection,
            result_for(p, selection, strategy_artifact=changed),
        )


def test_aud03_refit_cannot_include_any_test_period_information() -> None:
    p = protocol(refit=RefitPolicy.REFIT_TRAIN_VALIDATION)
    selection = selected(p)
    leaked = artifact(
        fitted="8" * 64,
        fit_at=p.test.start_at,
        created_at=p.test.start_at,
    )
    with pytest.raises(CausalityViolation, match="test/future"):
        bind_held_out_test(
            p,
            selection,
            result_for(p, selection, strategy_artifact=leaked),
        )


def test_aud03_original_evidence_mutation_cannot_rewrite_bound_chronology() -> None:
    p = protocol()
    selection = selected(p)
    result = result_for(p, selection)
    assessment = bind_held_out_test(p, selection, result)

    object.__setattr__(selection, "selected_at", p.test.start_at + timedelta(days=1))
    object.__setattr__(result, "evaluated_at", p.test.start_at)
    assert assessment.require_promotion_metric() == Decimal("0.75")


def test_aud03_bound_snapshot_mutation_invalidates_promotion_authority() -> None:
    p = protocol()
    selection = selected(p)
    assessment = bind_held_out_test(p, selection, result_for(p, selection))
    object.__setattr__(assessment.test_result, "metric_value", Decimal("999"))
    with pytest.raises(TradingResearchError, match="changed after construction"):
        assessment.require_promotion_metric()


def test_aud03_malformed_and_nonfinite_values_fail_closed() -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        CandidateScore(
            artifact(),
            Partition.VALIDATION,
            "sharpe",
            METRIC,
            Decimal("NaN"),
            DATASET,
            CLEAN,
            UNIVERSE,
            BASE + timedelta(days=9),
            BASE + timedelta(days=15),
        )
    with pytest.raises(TradingResearchError, match="datetime"):
        StrategyArtifactFingerprint(
            "candidate",
            "v1",
            ALGORITHM,
            CONFIG,
            FEATURES,
            FITTED,
            17,
            "not-a-time",  # type: ignore[arg-type]
            BASE + timedelta(days=9),
        )


def test_aud03_dev26_has_no_network_broker_or_order_execution_import() -> None:
    banned = {"broker", "httpx", "requests", "socket", "urllib"}
    for module in (heldout, metrics):
        tree = ast.parse(inspect.getsource(module))
        roots = {
            alias.name.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        roots.update(
            node.module.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not (roots & banned)
