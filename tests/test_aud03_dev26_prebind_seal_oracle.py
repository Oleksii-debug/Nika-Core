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
CLEAN = ReplayDataQuality(0, 0, 0, "d" * 64)


def _protocol() -> HeldOutProtocol:
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


def _artifact() -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        "aud03",
        "v1",
        "e" * 64,
        "f" * 64,
        "1" * 64,
        "2" * 64,
        11,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


def _score() -> CandidateScore:
    return CandidateScore(
        _artifact(),
        Partition.VALIDATION,
        "sharpe",
        "c" * 64,
        Decimal("1.1"),
        "a" * 64,
        CLEAN,
        "b" * 64,
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def test_aud03_candidate_timestamp_mutation_cannot_gain_selection_authority() -> None:
    p = _protocol()
    candidate = _score()
    object.__setattr__(candidate, "evaluated_at", p.test.start_at)
    with pytest.raises(TradingResearchError, match="candidate score evidence changed"):
        select_validation_candidate(p, (candidate,), selected_at=p.test.start_at)


def test_aud03_selection_timestamp_mutation_cannot_gain_bind_authority() -> None:
    p = _protocol()
    selected = select_validation_candidate(
        p,
        (_score(),),
        selected_at=p.validation.end_at,
    )
    object.__setattr__(selected, "selected_at", p.test.start_at)
    heldout = PartitionResult(
        selected.strategy_artifact,
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
    with pytest.raises(TradingResearchError, match="selection evidence changed"):
        bind_held_out_test(p, selected, heldout)


def test_aud03_protocol_mutation_cannot_redefine_partition_chronology() -> None:
    p = _protocol()
    object.__setattr__(p.validation, "end_at", p.validation.end_at + timedelta(hours=1))
    with pytest.raises(TradingResearchError, match="protocol evidence changed"):
        select_validation_candidate(p, (_score(),), selected_at=p.test.start_at)
