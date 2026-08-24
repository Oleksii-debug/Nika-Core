import hashlib
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
QUALITY = ReplayDataQuality(0, 0, 0, "d" * 64)


def artifact(strategy_id: str, strategy_version: str) -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        strategy_id,
        strategy_version,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "e" * 64,
        13,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


def legacy_digest(value: StrategyArtifactFingerprint) -> str:
    preimage = "|".join(
        (
            "nika-trader-strategy-artifact-v1",
            value.strategy_id,
            value.strategy_version,
            value.algorithm_sha256,
            value.config_sha256,
            value.feature_pipeline_sha256,
            value.fitted_state_sha256,
            str(value.seed),
            value.fit_cutoff_at.isoformat(),
            value.created_at.isoformat(),
        )
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


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


def selection(p: HeldOutProtocol, value: StrategyArtifactFingerprint):
    score = CandidateScore(
        value,
        Partition.VALIDATION,
        "sharpe",
        "f" * 64,
        Decimal(1),
        "1" * 64,
        QUALITY,
        "2" * 64,
        BASE + timedelta(days=9),
        p.validation.end_at,
    )
    return select_validation_candidate(p, (score,), selected_at=p.validation.end_at)


def test_aud03_strategy_identity_delimiter_collision_is_separated() -> None:
    original = artifact("alpha|beta", "gamma")
    replacement = artifact("alpha", "beta|gamma")

    assert legacy_digest(original) == legacy_digest(replacement)
    assert original.fingerprint != replacement.fingerprint


def test_aud03_legacy_colliding_artifact_cannot_replace_selected_strategy() -> None:
    p = protocol()
    original = artifact("alpha|beta", "gamma")
    replacement = artifact("alpha", "beta|gamma")
    selected = selection(p, original)
    assert legacy_digest(original) == legacy_digest(replacement)

    object.__setattr__(selected, "strategy_artifact", replacement)
    result = PartitionResult(
        original,
        Partition.TEST,
        selected.metric_name,
        selected.metric_fingerprint,
        Decimal(1),
        selected.dataset_semantic_hash,
        QUALITY,
        selected.universe_fingerprint,
        selected.universe_cutoff_at,
        p.test.end_at,
    )

    with pytest.raises(TradingResearchError, match="strategy artifact identity changed"):
        bind_held_out_test(p, selected, result)
