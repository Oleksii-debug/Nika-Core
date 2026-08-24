import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research.contracts import Partition, TradingResearchError
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutProtocol,
    PartitionWindow,
    ReplayDataQuality,
    StrategyArtifactFingerprint,
    select_validation_candidate,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64


def artifact(strategy_id: str, strategy_version: str) -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        strategy_id,
        strategy_version,
        DIGEST_A,
        DIGEST_B,
        DIGEST_C,
        DIGEST_D,
        7,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


def legacy_fingerprint(value: StrategyArtifactFingerprint) -> str:
    payload = "|".join(
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
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def score(strategy_artifact: StrategyArtifactFingerprint) -> CandidateScore:
    return CandidateScore(
        strategy_artifact,
        Partition.VALIDATION,
        "sharpe",
        "e" * 64,
        Decimal(5) / Decimal(4),
        "f" * 64,
        ReplayDataQuality(0, 0, 0, "1" * 64),
        "2" * 64,
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
    )


def test_strategy_fingerprint_framing_separates_legacy_delimiter_collision() -> None:
    left = artifact("alpha|beta", "gamma")
    right = artifact("alpha", "beta|gamma")

    assert legacy_fingerprint(left) == legacy_fingerprint(right)
    assert left.fingerprint != right.fingerprint


def test_colliding_legacy_artifact_cannot_replace_sealed_candidate_evidence() -> None:
    original = artifact("alpha|beta", "gamma")
    replacement = artifact("alpha", "beta|gamma")
    candidate = score(original)
    assert legacy_fingerprint(original) == legacy_fingerprint(replacement)

    object.__setattr__(candidate, "strategy_artifact", replacement)

    with pytest.raises(TradingResearchError, match="strategy artifact changed"):
        select_validation_candidate(
            protocol(),
            (candidate,),
            selected_at=BASE + timedelta(days=15),
        )
