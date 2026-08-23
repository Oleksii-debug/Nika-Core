from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research.contracts import CausalityViolation, Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutAssessment,
    HeldOutProtocol,
    PartitionResult,
    PartitionWindow,
    ReplayDataQuality,
    SelectionDecision,
    bind_held_out_test,
    select_validation_candidate,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64
UNIVERSE = "b" * 64
CLEAN = ReplayDataQuality(0, 0, 0)


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


def score(strategy_id: str = "chosen") -> CandidateScore:
    return CandidateScore(
        strategy_id,
        Partition.VALIDATION,
        "sharpe",
        Decimal(1),
        HASH,
        CLEAN,
        UNIVERSE,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def result_for(selection: SelectionDecision) -> PartitionResult:
    p = protocol()
    return PartitionResult(
        selection.strategy_id,
        Partition.TEST,
        selection.metric_name,
        Decimal("0.5"),
        selection.dataset_semantic_hash,
        CLEAN,
        selection.universe_fingerprint,
        p.test.start_at,
        selection.universe_cutoff_at,
        p.test.end_at,
    )


def test_selection_and_assessment_are_factory_only_evidence() -> None:
    with pytest.raises(TypeError, match="select_validation_candidate"):
        SelectionDecision()
    with pytest.raises(TypeError, match="bind_held_out_test"):
        HeldOutAssessment()


def test_bind_revalidates_corrupt_selection_chronology_and_universe_cutoff() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p, (score(),), selected_at=p.validation.end_at
    )
    result = result_for(selected)

    object.__setattr__(selected, "selected_at", p.validation.end_at - timedelta(seconds=1))
    with pytest.raises(CausalityViolation, match="validation completion"):
        bind_held_out_test(p, selected, result)

    object.__setattr__(selected, "selected_at", p.validation.end_at)
    object.__setattr__(selected, "universe_cutoff_at", p.validation.start_at)
    with pytest.raises(CausalityViolation, match="not fixed before validation"):
        bind_held_out_test(p, selected, result)


def test_canonical_identity_digest_metric_and_direction_fail_closed() -> None:
    with pytest.raises(TradingResearchError, match="canonical non-empty identity"):
        score(" chosen")
    with pytest.raises(TradingResearchError, match="lowercase SHA-256"):
        CandidateScore(
            "chosen",
            Partition.VALIDATION,
            "sharpe",
            Decimal(1),
            "A" * 64,
            CLEAN,
            UNIVERSE,
            BASE + timedelta(days=9),
            BASE + timedelta(days=9),
            BASE + timedelta(days=15),
        )
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        CandidateScore(
            "chosen",
            Partition.VALIDATION,
            "sharpe",
            1,  # type: ignore[arg-type]
            HASH,
            CLEAN,
            UNIVERSE,
            BASE + timedelta(days=9),
            BASE + timedelta(days=9),
            BASE + timedelta(days=15),
        )
    with pytest.raises(TradingResearchError, match="boolean"):
        select_validation_candidate(
            protocol(),
            (score(),),
            selected_at=protocol().validation.end_at,
            higher_is_better=1,  # type: ignore[arg-type]
        )


def test_universe_must_be_fixed_strictly_before_validation() -> None:
    p = protocol()
    boundary_universe = CandidateScore(
        "boundary",
        Partition.VALIDATION,
        "sharpe",
        Decimal(1),
        HASH,
        CLEAN,
        UNIVERSE,
        BASE + timedelta(days=9),
        p.validation.start_at,
        p.validation.end_at,
    )
    with pytest.raises(CausalityViolation, match="fixed before validation"):
        select_validation_candidate(
            p, (boundary_universe,), selected_at=p.validation.end_at
        )


def test_selection_at_shared_half_open_boundary_remains_deterministic() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.test.start_at,
    )
    assert selected.selected_at == p.test.start_at


def test_promotion_metric_revalidates_assessment_identity_after_corruption() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p, (score(),), selected_at=p.validation.end_at
    )
    result = result_for(selected)
    assessment = bind_held_out_test(p, selected, result)
    object.__setattr__(result, "dataset_semantic_hash", "c" * 64)
    with pytest.raises(TradingResearchError, match="dataset identity changed"):
        assessment.require_promotion_metric()
