from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_research_metric_evidence_helpers import (
    synthetic_daily_sampling,
    total_return_evidence,
)

from nika_core.trading_research.contracts import Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutProtocol,
    PartitionWindow,
    ReplayDataQuality,
    StrategyArtifactFingerprint,
    select_validation_candidate,
)
from nika_core.trading_research.metric_evidence import (
    MetricEvidence,
    calculate_metric_evidence,
    validate_metric_evidence,
)
from nika_core.trading_research.metrics import EquityPoint, SamplingMode, SamplingSpec

BASE = datetime(2026, 1, 1, tzinfo=UTC)
CLEAN = ReplayDataQuality(0, 0, 0, "d" * 64)


def artifact(strategy_id: str) -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        strategy_id,
        "v1",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "e" * 64,
        7,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


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
            BASE + timedelta(days=16),
            BASE + timedelta(days=20),
        ),
    )


def candidate(strategy_id: str, evidence: MetricEvidence) -> CandidateScore:
    return CandidateScore(
        artifact(strategy_id),
        Partition.VALIDATION,
        evidence.metric_name,
        evidence.definition_sha256,
        evidence.value,
        "f" * 64,
        CLEAN,
        "1" * 64,
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
        metric_evidence=evidence,
    )


def test_metric_evidence_is_factory_only_and_binds_exact_trace() -> None:
    with pytest.raises(TypeError, match="calculate_metric_evidence"):
        MetricEvidence()

    direct = total_return_evidence(BASE, "0.5")
    path_changed = total_return_evidence(BASE, "0.5", middle="80")

    assert direct.value == path_changed.value == Decimal("0.5")
    assert direct.definition_sha256 == path_changed.definition_sha256
    assert direct.sampling_fingerprint == path_changed.sampling_fingerprint
    assert direct.equity_trace_sha256 != path_changed.equity_trace_sha256
    assert direct.return_trace_sha256 != path_changed.return_trace_sha256
    assert direct.evidence_sha256 != path_changed.evidence_sha256


def test_metric_evidence_mutation_fails_closed() -> None:
    evidence = total_return_evidence(BASE, "0.5")
    object.__setattr__(evidence, "value", Decimal(999))
    with pytest.raises(TradingResearchError, match="changed after construction"):
        validate_metric_evidence(evidence)


def test_candidate_rejects_missing_or_spoofed_metric_authority() -> None:
    evidence = total_return_evidence(BASE, "0.5")
    with pytest.raises(TradingResearchError, match="metric_evidence is required"):
        CandidateScore(
            artifact("missing"),
            Partition.VALIDATION,
            evidence.metric_name,
            evidence.definition_sha256,
            evidence.value,
            "f" * 64,
            CLEAN,
            "1" * 64,
            BASE + timedelta(days=9),
            BASE + timedelta(days=15),
        )
    with pytest.raises(TradingResearchError, match="metric value"):
        CandidateScore(
            artifact("spoof"),
            Partition.VALIDATION,
            evidence.metric_name,
            evidence.definition_sha256,
            Decimal(999),
            "f" * 64,
            CLEAN,
            "1" * 64,
            BASE + timedelta(days=9),
            BASE + timedelta(days=15),
            metric_evidence=evidence,
        )


def test_candidate_evidence_mutation_after_binding_fails_selection() -> None:
    p = protocol()
    bound = candidate("chosen", total_return_evidence(BASE, "0.5"))
    assert bound.metric_evidence is not None
    object.__setattr__(bound.metric_evidence, "equity_trace_sha256", "9" * 64)
    with pytest.raises(TradingResearchError, match="changed after construction"):
        select_validation_candidate(
            p,
            (bound,),
            selected_at=p.validation.end_at,
        )


def test_selection_rejects_sampling_or_rate_assumption_substitution() -> None:
    p = protocol()
    baseline = total_return_evidence(BASE, "0.5")
    continuous_sampling = SamplingSpec(
        SamplingMode.REGULAR,
        "continuous-utc-daily-v1",
        timedelta(days=1),
        Decimal(252),
    )
    different_sampling = calculate_metric_evidence(
        (
            EquityPoint(BASE, Decimal(100)),
            EquityPoint(BASE + timedelta(days=1), Decimal(160)),
        ),
        trade_count=1,
        sampling=continuous_sampling,
        metric_name="total_return",
    )
    with pytest.raises(TradingResearchError, match="sampling contract"):
        select_validation_candidate(
            p,
            (candidate("a", baseline), candidate("b", different_sampling)),
            selected_at=p.validation.end_at,
        )

    different_rate = calculate_metric_evidence(
        (
            EquityPoint(BASE, Decimal(100)),
            EquityPoint(BASE + timedelta(days=1), Decimal(160)),
        ),
        trade_count=1,
        sampling=synthetic_daily_sampling(),
        metric_name="total_return",
        risk_free_rate_per_period=Decimal("0.01"),
    )
    with pytest.raises(TradingResearchError, match="risk-free assumptions"):
        select_validation_candidate(
            p,
            (candidate("a", baseline), candidate("b", different_rate)),
            selected_at=p.validation.end_at,
        )
