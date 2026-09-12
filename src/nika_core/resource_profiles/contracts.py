from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResourceProfileName(StrEnum):
    """Stable user-facing resource profile identifiers."""

    NORMAL = "normal"
    ECONOMY = "economy"
    NIGHT_BATCH = "night_batch"
    LOW_MEMORY = "low_memory"


class WorkloadClass(StrEnum):
    """Coarse workload classes used only for admission policy."""

    GENERAL = "general"
    CHROMIUM = "chromium"
    LOCAL_MODEL = "local_model"
    TRANSCRIPTION = "transcription"
    HEAVY_BATCH = "heavy_batch"


@dataclass(frozen=True, slots=True)
class ResourceProfileSpec:
    """Immutable policy limits for one named resource profile."""

    name: ResourceProfileName
    max_cpu_percent: float
    max_memory_percent: float
    min_available_memory_bytes: int
    allowed_workloads: frozenset[WorkloadClass]
    max_simultaneous_heavy_workloads: int = 1
    recommend_idle_model_unload: bool = False


@dataclass(frozen=True, slots=True)
class ProfileDecision:
    """Deterministic, text-first result suitable for logs and accessible UI."""

    allowed: bool
    reason: str
    profile: str
    requested_workload: str
    recommendations: tuple[str, ...] = ()
