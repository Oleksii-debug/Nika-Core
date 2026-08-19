from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence

from .contracts import CausalityViolation, Partition, require_aware_utc


@dataclass(frozen=True, slots=True)
class FeaturePoint:
    value: Decimal | None
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", require_aware_utc(self.available_at, "available_at"))


@dataclass(frozen=True, slots=True)
class FeatureLineage:
    name: str
    input_names: tuple[str, ...]
    input_available_at: tuple[datetime, ...]
    available_at: datetime

    def __post_init__(self) -> None:
        inputs = tuple(
            require_aware_utc(value, "input_available_at") for value in self.input_available_at
        )
        available_at = require_aware_utc(self.available_at, "available_at")
        if inputs and available_at < max(inputs):
            raise CausalityViolation("derived feature cannot be available before its latest input")
        object.__setattr__(self, "input_available_at", inputs)
        object.__setattr__(self, "available_at", available_at)


def causal_shift(points: Sequence[FeaturePoint], periods: int) -> tuple[FeaturePoint, ...]:
    if periods < 0:
        raise CausalityViolation("negative shift leaks future values")
    if periods == 0:
        return tuple(points)
    result: list[FeaturePoint] = []
    for index, point in enumerate(points):
        if index < periods:
            result.append(FeaturePoint(None, point.available_at))
        else:
            source = points[index - periods]
            result.append(FeaturePoint(source.value, point.available_at))
    return tuple(result)


def trailing_mean(
    points: Sequence[FeaturePoint], window: int, *, centered: bool = False
) -> tuple[FeaturePoint, ...]:
    if centered:
        raise CausalityViolation("centered rolling windows use future observations")
    if window <= 0:
        raise ValueError("window must be positive")
    result: list[FeaturePoint] = []
    for index, point in enumerate(points):
        start = max(0, index - window + 1)
        values = [p.value for p in points[start : index + 1] if p.value is not None]
        mean = None if not values else sum(values, Decimal("0")) / Decimal(len(values))
        result.append(FeaturePoint(mean, point.available_at))
    return tuple(result)


def fill_missing(
    points: Sequence[FeaturePoint], *, method: str = "forward"
) -> tuple[FeaturePoint, ...]:
    if method != "forward":
        raise CausalityViolation("only forward fill is causal; backward fill is forbidden")
    last: Decimal | None = None
    result: list[FeaturePoint] = []
    for point in points:
        if point.value is not None:
            last = point.value
        result.append(FeaturePoint(last, point.available_at))
    return tuple(result)


class TrainOnlyStandardizer:
    __slots__ = ("_mean", "_scale", "_fitted")

    def __init__(self) -> None:
        self._mean = Decimal("0")
        self._scale = Decimal("1")
        self._fitted = False

    def fit(self, values: Iterable[Decimal], *, partition: Partition) -> None:
        if partition is not Partition.TRAIN:
            raise CausalityViolation("feature/scaler fitting is allowed only on the train partition")
        materialized = tuple(values)
        if not materialized:
            raise ValueError("cannot fit empty values")
        mean = sum(materialized, Decimal("0")) / Decimal(len(materialized))
        variance = sum((value - mean) ** 2 for value in materialized) / Decimal(len(materialized))
        self._mean = mean
        self._scale = variance.sqrt() if variance > 0 else Decimal("1")
        self._fitted = True

    def transform(self, values: Iterable[Decimal]) -> tuple[Decimal, ...]:
        if not self._fitted:
            raise CausalityViolation("standardizer must be fit on train data before transform")
        return tuple((value - self._mean) / self._scale for value in values)


class AvailabilityCache:
    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[str, tuple[datetime, object]] = {}

    def put(self, key: str, value: object, *, available_at: datetime) -> None:
        self._values[key] = (require_aware_utc(available_at, "available_at"), value)

    def get(self, key: str, *, at: datetime) -> object | None:
        at = require_aware_utc(at, "at")
        stored = self._values.get(key)
        if stored is None:
            return None
        available_at, value = stored
        if available_at > at:
            raise CausalityViolation("future cache entry is not visible at decision time")
        return value
