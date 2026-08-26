from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research.contracts import Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutProtocol,
    PartitionResult,
    PartitionWindow,
    ReplayDataQuality,
    StrategyArtifactFingerprint,
    bind_held_out_test,
    select_validation_candidate,
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


def artifact() -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        "candidate",
        "v1",
        ALGORITHM,
        CONFIG,
        FEATURES,
        FITTED,
        7,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


def score() -> CandidateScore:
    return CandidateScore(
        artifact(),
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


def selection(p: HeldOutProtocol):
    return select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )


def result(p: HeldOutProtocol, selected) -> PartitionResult:
    return PartitionResult(
        selected.strategy_artifact,
        Partition.TEST,
        selected.metric_name,
        selected.metric_fingerprint,
        Decimal("0.75"),
        selected.dataset_semantic_hash,
        CLEAN,
        selected.universe_fingerprint,
        selected.universe_cutoff_at,
        p.test.end_at,
    )


def test_candidate_chronology_mutation_before_selection_is_rejected() -> None:
    p = protocol()
    candidate = score()
    object.__setattr__(candidate, "evaluated_at", p.test.start_at)
    with pytest.raises(TradingResearchError, match="candidate score evidence changed"):
        select_validation_candidate(
            p,
            (candidate,),
            selected_at=p.test.start_at,
        )


def test_selection_chronology_mutation_before_binding_is_rejected() -> None:
    p = protocol()
    selected = selection(p)
    object.__setattr__(selected, "selected_at", p.test.start_at)
    with pytest.raises(TradingResearchError, match="selection evidence changed"):
        bind_held_out_test(p, selected, result(p, selected))


def test_result_chronology_mutation_before_binding_is_rejected() -> None:
    p = protocol()
    selected = selection(p)
    heldout = result(p, selected)
    object.__setattr__(heldout, "evaluated_at", p.test.end_at + timedelta(days=1))
    with pytest.raises(TradingResearchError, match="partition result evidence changed"):
        bind_held_out_test(p, selected, heldout)


def test_protocol_window_mutation_before_selection_is_rejected() -> None:
    p = protocol()
    object.__setattr__(p.validation, "end_at", p.validation.end_at + timedelta(hours=1))
    with pytest.raises(TradingResearchError, match="protocol evidence changed"):
        select_validation_candidate(
            p,
            (score(),),
            selected_at=p.test.start_at,
        )
