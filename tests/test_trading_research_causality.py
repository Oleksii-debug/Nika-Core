from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    AvailabilityCache,
    Bar,
    CausalityViolation,
    Dataset,
    EventTime,
    FeatureLineage,
    FeaturePoint,
    FutureAccessError,
    Instrument,
    Partition,
    Provenance,
    StrategyDecision,
    TrainOnlyStandardizer,
    Venue,
    causal_shift,
    fill_missing,
    trailing_mean,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
VENUE = Venue("sim", "Europe/Kyiv")
INSTRUMENT = Instrument("ABC", VENUE, "usd")
PROVENANCE = Provenance("fixture", acquired_at=BASE)


def bar(minutes: int, close: str, *, available_delay: int = 0, sequence: int = 0) -> Bar:
    event_at = BASE + timedelta(minutes=minutes)
    return Bar(
        INSTRUMENT,
        EventTime(event_at, event_at + timedelta(minutes=available_delay), event_at),
        Decimal(close),
        Decimal(close) + 1,
        Decimal(close) - 1,
        Decimal(close),
        Decimal(10),
        sequence,
    )


def visible_trace(events: list[Bar], through: datetime) -> tuple[tuple[str, str], ...]:
    view = Dataset("d", "1", events, PROVENANCE).temporal_view(through)
    return tuple((event.time.event_at.isoformat(), str(event.close)) for event in view)


def test_future_mutation_after_t_preserves_complete_visible_trace_through_t() -> None:
    baseline = [bar(0, "100"), bar(1, "101"), bar(2, "102"), bar(3, "103")]
    mutated = [bar(0, "100"), bar(1, "101"), bar(2, "999"), bar(3, "2")]
    through = BASE + timedelta(minutes=1)
    assert visible_trace(baseline, through) == visible_trace(mutated, through)
    assert Dataset("d", "1", baseline, PROVENANCE).temporal_view(through).trace_hash == Dataset(
        "d", "1", mutated, PROVENANCE
    ).temporal_view(through).trace_hash


def test_dataset_truncation_after_t_preserves_complete_visible_trace_through_t() -> None:
    full = [bar(0, "100"), bar(1, "101"), bar(2, "102"), bar(3, "103")]
    through = BASE + timedelta(minutes=1)
    assert visible_trace(full, through) == visible_trace(full[:2], through)


def test_temporal_view_rejects_dataframe_and_backing_storage_escape_hatches() -> None:
    view = Dataset("d", "1", [bar(0, "100"), bar(2, "102")], PROVENANCE).temporal_view(BASE)
    assert len(view) == 1
    for name in ("iloc", "loc", "index", "dataset", "events", "backing", "raw", "values"):
        with pytest.raises(FutureAccessError):
            getattr(view, name)
    assert not hasattr(view, "__dict__")
    with pytest.raises(FutureAccessError):
        view.require_available(BASE + timedelta(seconds=1))


def test_available_at_not_event_at_controls_visibility() -> None:
    delayed = bar(0, "100", available_delay=5)
    dataset = Dataset("d", "1", [delayed], PROVENANCE)
    assert len(dataset.temporal_view(BASE + timedelta(minutes=4))) == 0
    assert len(dataset.temporal_view(BASE + timedelta(minutes=5))) == 1


def test_adversarial_noncausal_transforms_fail_closed() -> None:
    points = (
        FeaturePoint(Decimal(1), BASE),
        FeaturePoint(None, BASE + timedelta(minutes=1)),
        FeaturePoint(Decimal(3), BASE + timedelta(minutes=2)),
    )
    with pytest.raises(CausalityViolation, match="negative shift"):
        causal_shift(points, -1)
    with pytest.raises(CausalityViolation, match="centered"):
        trailing_mean(points, 3, centered=True)
    with pytest.raises(CausalityViolation, match="backward"):
        fill_missing(points, method="backward")


def test_train_only_fit_blocks_validation_and_test_leakage() -> None:
    scaler = TrainOnlyStandardizer()
    for partition in (Partition.VALIDATION, Partition.TEST):
        with pytest.raises(CausalityViolation, match="train"):
            scaler.fit([Decimal(1), Decimal(2)], partition=partition)
    scaler.fit([Decimal(1), Decimal(2)], partition=Partition.TRAIN)
    assert len(scaler.transform([Decimal(3)])) == 1


def test_feature_lineage_cannot_precede_latest_input() -> None:
    with pytest.raises(CausalityViolation):
        FeatureLineage(
            "bad",
            ("x", "y"),
            (BASE, BASE + timedelta(minutes=2)),
            BASE + timedelta(minutes=1),
        )


def test_future_cache_entry_is_not_visible() -> None:
    cache = AvailabilityCache()
    cache.put("f", StrategyDecision("hold"), available_at=BASE + timedelta(minutes=2))
    with pytest.raises(CausalityViolation, match="future cache"):
        cache.get("f", at=BASE + timedelta(minutes=1))
