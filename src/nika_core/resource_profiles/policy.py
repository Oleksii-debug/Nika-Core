from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite
from types import MappingProxyType

from nika_core.resource_profiles.contracts import (
    ProfileDecision,
    ResourceProfileName,
    ResourceProfileSpec,
    WorkloadClass,
)
from nika_core.resources.contracts import ResourceSnapshot

_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_MAX_SIGNED_64 = (1 << 63) - 1
_ALL_WORKLOADS = frozenset(WorkloadClass)
_HEAVY_WORKLOADS = frozenset(
    {
        WorkloadClass.CHROMIUM,
        WorkloadClass.LOCAL_MODEL,
        WorkloadClass.TRANSCRIPTION,
        WorkloadClass.HEAVY_BATCH,
    }
)

_DEFAULT_PROFILES: Mapping[ResourceProfileName, ResourceProfileSpec] = MappingProxyType(
    {
        ResourceProfileName.NORMAL: ResourceProfileSpec(
            name=ResourceProfileName.NORMAL,
            max_cpu_percent=95.0,
            max_memory_percent=90.0,
            min_available_memory_bytes=512 * _MIB,
            allowed_workloads=_ALL_WORKLOADS,
        ),
        ResourceProfileName.ECONOMY: ResourceProfileSpec(
            name=ResourceProfileName.ECONOMY,
            max_cpu_percent=75.0,
            max_memory_percent=80.0,
            min_available_memory_bytes=1 * _GIB,
            allowed_workloads=_ALL_WORKLOADS,
            recommend_idle_model_unload=True,
        ),
        ResourceProfileName.NIGHT_BATCH: ResourceProfileSpec(
            name=ResourceProfileName.NIGHT_BATCH,
            max_cpu_percent=90.0,
            max_memory_percent=88.0,
            min_available_memory_bytes=1 * _GIB,
            allowed_workloads=_ALL_WORKLOADS,
        ),
        ResourceProfileName.LOW_MEMORY: ResourceProfileSpec(
            name=ResourceProfileName.LOW_MEMORY,
            max_cpu_percent=70.0,
            max_memory_percent=70.0,
            min_available_memory_bytes=2 * _GIB,
            allowed_workloads=frozenset({WorkloadClass.GENERAL}),
            recommend_idle_model_unload=True,
        ),
    }
)


class ResourceProfilePolicy:
    """Pure admission policy layered above Nika's existing ResourceManager."""

    def __init__(
        self,
        profiles: Mapping[ResourceProfileName, ResourceProfileSpec] | None = None,
    ) -> None:
        selected = dict(_DEFAULT_PROFILES if profiles is None else profiles)
        if not selected:
            raise ValueError("at least one resource profile is required")
        for name, spec in selected.items():
            _validate_profile(name, spec)
        self._profiles: Mapping[ResourceProfileName, ResourceProfileSpec] = MappingProxyType(
            selected
        )

    def profile_spec(self, profile: ResourceProfileName | str) -> ResourceProfileSpec:
        resolved = _coerce_profile(profile)
        if resolved is None or resolved not in self._profiles:
            raise ValueError(f"unknown resource profile: {profile!r}")
        return self._profiles[resolved]

    def evaluate(
        self,
        *,
        profile: ResourceProfileName | str,
        snapshot: ResourceSnapshot,
        requested_workload: WorkloadClass | str,
        active_workloads: Iterable[WorkloadClass | str] = (),
    ) -> ProfileDecision:
        profile_name = _coerce_profile(profile)
        requested = _coerce_workload(requested_workload)
        raw_profile = _raw_value(profile)
        raw_requested = _raw_value(requested_workload)

        if profile_name is None or profile_name not in self._profiles:
            return ProfileDecision(False, "unknown_profile", raw_profile, raw_requested)
        if requested is None:
            return ProfileDecision(False, "unknown_workload", profile_name.value, raw_requested)

        active: list[WorkloadClass] = []
        for workload in active_workloads:
            resolved = _coerce_workload(workload)
            if resolved is None:
                return ProfileDecision(
                    False,
                    "unknown_active_workload",
                    profile_name.value,
                    requested.value,
                )
            active.append(resolved)

        spec = self._profiles[profile_name]
        recommendations = _recommendations(spec)
        if not _valid_snapshot(snapshot):
            return ProfileDecision(
                False,
                "invalid_resource_snapshot",
                profile_name.value,
                requested.value,
                recommendations,
            )
        if requested not in spec.allowed_workloads:
            return ProfileDecision(
                False,
                "profile_blocks_workload",
                profile_name.value,
                requested.value,
                recommendations,
            )
        if snapshot.cpu_percent > spec.max_cpu_percent:
            return ProfileDecision(
                False,
                "cpu_pressure",
                profile_name.value,
                requested.value,
                recommendations,
            )
        if snapshot.memory_percent > spec.max_memory_percent:
            return ProfileDecision(
                False,
                "memory_pressure",
                profile_name.value,
                requested.value,
                recommendations,
            )
        if snapshot.available_memory_bytes < spec.min_available_memory_bytes:
            return ProfileDecision(
                False,
                "available_memory_floor",
                profile_name.value,
                requested.value,
                recommendations,
            )
        if requested in _HEAVY_WORKLOADS:
            heavy_active = sum(workload in _HEAVY_WORKLOADS for workload in active)
            if heavy_active >= spec.max_simultaneous_heavy_workloads:
                return ProfileDecision(
                    False,
                    "heavy_workload_conflict",
                    profile_name.value,
                    requested.value,
                    recommendations,
                )

        return ProfileDecision(True, "profile_allows", profile_name.value, requested.value)


def _validate_profile(name: ResourceProfileName, spec: ResourceProfileSpec) -> None:
    if name is not spec.name:
        raise ValueError("resource profile mapping key must match spec.name")
    for field_name, value in (
        ("max_cpu_percent", spec.max_cpu_percent),
        ("max_memory_percent", spec.max_memory_percent),
    ):
        if not _is_percentage(value, allow_zero=False):
            raise ValueError(f"{field_name} must be a finite number in the range (0, 100]")
    if not _is_nonnegative_int(spec.min_available_memory_bytes):
        raise ValueError("min_available_memory_bytes must be a bounded non-negative integer")
    if not _is_positive_int(spec.max_simultaneous_heavy_workloads):
        raise ValueError("max_simultaneous_heavy_workloads must be a bounded positive integer")
    if type(spec.allowed_workloads) is not frozenset or not spec.allowed_workloads:
        raise ValueError("allowed_workloads must be a non-empty immutable frozenset")
    if any(type(workload) is not WorkloadClass for workload in spec.allowed_workloads):
        raise ValueError("allowed_workloads must contain only WorkloadClass values")
    if type(spec.recommend_idle_model_unload) is not bool:
        raise ValueError("recommend_idle_model_unload must be a boolean")


def _valid_snapshot(snapshot: ResourceSnapshot) -> bool:
    return (
        _is_percentage(snapshot.cpu_percent, allow_zero=True)
        and _is_percentage(snapshot.memory_percent, allow_zero=True)
        and _is_nonnegative_int(snapshot.available_memory_bytes)
    )


def _is_percentage(value: object, *, allow_zero: bool) -> bool:
    if type(value) is int:
        return (0 <= value <= 100) if allow_zero else (0 < value <= 100)
    if type(value) is float:
        if not isfinite(value):
            return False
        return (0 <= value <= 100) if allow_zero else (0 < value <= 100)
    return False


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SIGNED_64


def _is_positive_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _MAX_SIGNED_64


def _coerce_profile(value: ResourceProfileName | str) -> ResourceProfileName | None:
    try:
        return ResourceProfileName(value)
    except (TypeError, ValueError):
        return None


def _coerce_workload(value: WorkloadClass | str) -> WorkloadClass | None:
    try:
        return WorkloadClass(value)
    except (TypeError, ValueError):
        return None


def _raw_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _recommendations(spec: ResourceProfileSpec) -> tuple[str, ...]:
    if spec.recommend_idle_model_unload:
        return ("unload_idle_local_model_if_safe",)
    return ()
