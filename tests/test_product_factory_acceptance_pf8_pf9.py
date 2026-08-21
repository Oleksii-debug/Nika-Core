from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    SQLiteExperimentRepository,
    StrategyRef,
)


def _strategy(
    candidate_id: str,
    *,
    artifact_kind: ArtifactKind,
    artifact_ref: str,
    permission_fingerprint: str = "business-sandbox-v1",
) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=artifact_kind,
        artifact_ref=artifact_ref,
        permission_fingerprint=permission_fingerprint,
    )


def _record_business_metrics(
    engine: ExperimentEngine,
    experiment_id: str,
    candidate_id: str,
    *,
    conversion: tuple[float, float, float],
    compliance: tuple[float, float, float],
) -> None:
    for replay_id, conversion_value, compliance_value in zip(
        ("segment-a", "segment-b", "segment-c"),
        conversion,
        compliance,
        strict=True,
    ):
        engine.record(
            experiment_id,
            MetricObservation(candidate_id, replay_id, "conversion", conversion_value),
        )
        engine.record(
            experiment_id,
            MetricObservation(candidate_id, replay_id, "compliance", compliance_value),
        )


def _business_definition(*, challenger_compliance_permission: str = "business-sandbox-v1") -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="pf9-business-pricing",
        champion=_strategy(
            "pricing-v1",
            artifact_kind=ArtifactKind.CONFIG,
            artifact_ref="business://pricing/v1",
        ),
        challengers=(
            _strategy(
                "pricing-v2",
                artifact_kind=ArtifactKind.CONFIG,
                artifact_ref="business://pricing/v2",
                permission_fingerprint=challenger_compliance_permission,
            ),
        ),
        replays=(
            ReplayCase("segment-a", "dataset://business/segments", "v1"),
            ReplayCase("segment-b", "dataset://business/segments", "v1"),
            ReplayCase("segment-c", "dataset://business/segments", "v1"),
        ),
        policy=PromotionPolicy(
            primary_metric="conversion",
            minimum_improvement=0.05,
            minimum_replays=3,
            guardrails=(MetricRule("compliance", max_regression=0.0),),
        ),
    )


def _sqlite_engine(path: Path) -> tuple[SQLiteExperimentRepository, ExperimentEngine]:
    store = SQLiteStore(path)
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    return repository, ExperimentEngine(repository)


def test_pf9_business_config_experiment_is_durable_promotable_and_rollback_safe(
    tmp_path: Path,
) -> None:
    """Existing Experiment Engine is a reusable PF9 sandbox primitive, not a second factory."""
    path = tmp_path / "nika.db"
    _, first = _sqlite_engine(path)
    first.create(_business_definition())
    first.start("pf9-business-pricing")
    _record_business_metrics(
        first,
        "pf9-business-pricing",
        "pricing-v1",
        conversion=(0.10, 0.11, 0.09),
        compliance=(1.0, 1.0, 1.0),
    )

    repository, restarted = _sqlite_engine(path)
    recovered = repository.get("pf9-business-pricing")
    assert recovered.status is ExperimentStatus.RUNNING
    assert len(recovered.observations) == 6

    _record_business_metrics(
        restarted,
        "pf9-business-pricing",
        "pricing-v2",
        conversion=(0.20, 0.21, 0.19),
        compliance=(1.0, 1.0, 1.0),
    )
    promoted = restarted.complete("pf9-business-pricing")
    assert promoted.status is ExperimentStatus.PROMOTED
    assert promoted.selected_candidate_id == "pricing-v2"

    _, final_process = _sqlite_engine(path)
    rolled_back = final_process.rollback("pf9-business-pricing")
    assert rolled_back.status is ExperimentStatus.ROLLED_BACK
    assert rolled_back.selected_candidate_id == "pricing-v1"


def test_pf9_business_candidate_cannot_expand_the_sandbox_permission_boundary() -> None:
    with pytest.raises(PermissionError, match="may not widen or alter permissions"):
        _business_definition(challenger_compliance_permission="business-sandbox-plus-network")


def test_pf9_conversion_gain_does_not_promote_when_compliance_guardrail_regresses(
    tmp_path: Path,
) -> None:
    _, engine = _sqlite_engine(tmp_path / "nika.db")
    engine.create(_business_definition())
    engine.start("pf9-business-pricing")
    _record_business_metrics(
        engine,
        "pf9-business-pricing",
        "pricing-v1",
        conversion=(0.10, 0.10, 0.10),
        compliance=(1.0, 1.0, 1.0),
    )
    _record_business_metrics(
        engine,
        "pf9-business-pricing",
        "pricing-v2",
        conversion=(0.30, 0.30, 0.30),
        compliance=(0.8, 0.8, 0.8),
    )

    completed = engine.complete("pf9-business-pricing")
    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == "pricing-v1"


def test_pf8_repair_candidate_experiment_cannot_widen_maintenance_permissions() -> None:
    """The experiment kernel can safely evaluate a repair, but is not the PF8 incident lifecycle."""
    champion = _strategy(
        "repair-baseline",
        artifact_kind=ArtifactKind.STRATEGY,
        artifact_ref="repair://service/baseline",
        permission_fingerprint="maintenance-read-test",
    )
    challenger = _strategy(
        "repair-candidate",
        artifact_kind=ArtifactKind.STRATEGY,
        artifact_ref="repair://service/candidate",
        permission_fingerprint="maintenance-read-test-deploy",
    )

    with pytest.raises(PermissionError, match="may not widen or alter permissions"):
        ExperimentDefinition(
            experiment_id="pf8-repair-sandbox",
            champion=champion,
            challengers=(challenger,),
            replays=(ReplayCase("incident-replay", "dataset://incident/replay", "v1"),),
            policy=PromotionPolicy(primary_metric="recovery_quality"),
        )
