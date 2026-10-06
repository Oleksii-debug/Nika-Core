from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .contracts import CapabilityGap, ReuseCandidate

REUSE_SOURCE_ORDER = (
    "tool_registry",
    "plugin_registry",
    "mcp_metadata",
    "workspace_capabilities",
    "installed_distributions",
    "approved_catalog",
)


@runtime_checkable
class ReuseMetadataSource(Protocol):
    source_id: str

    def search(self, capability_id: str) -> tuple[ReuseCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class ReuseSearchResult:
    candidates: tuple[ReuseCandidate, ...]
    attempted_sources: tuple[str, ...]


class ReuseSearchPipeline:
    """Deterministically search capability metadata in the binding reuse order.

    Sources return metadata only. This layer never imports, installs, downloads, executes, or
    trusts candidate code. Permission widening is filtered before candidates can reach selection.
    """

    def __init__(self, sources: tuple[ReuseMetadataSource, ...]) -> None:
        by_id: dict[str, ReuseMetadataSource] = {}
        for source in sources:
            source_id = source.source_id.strip()
            if source_id not in REUSE_SOURCE_ORDER:
                raise ValueError(f"unsupported reuse metadata source: {source_id}")
            if source_id in by_id:
                raise ValueError(f"duplicate reuse metadata source: {source_id}")
            by_id[source_id] = source
        self._sources = by_id

    def search(self, gap: CapabilityGap) -> ReuseSearchResult:
        attempted: list[str] = []
        candidates: list[ReuseCandidate] = []
        for source_id in REUSE_SOURCE_ORDER:
            source = self._sources.get(source_id)
            if source is None:
                continue
            attempted.append(source_id)
            for candidate in source.search(gap.requested_capability):
                if candidate.source != source_id:
                    raise ValueError(
                        f"reuse metadata source {source_id} returned candidate claiming {candidate.source}"
                    )
                if candidate.capability_id != gap.requested_capability:
                    continue
                if not candidate.permissions.issubset(gap.permission_ceiling):
                    continue
                candidates.append(candidate)
        return ReuseSearchResult(tuple(candidates), tuple(attempted))


@dataclass(slots=True)
class StaticReuseMetadataSource:
    """Deterministic metadata-only fixture/adaptor useful for registry integration tests."""

    source_id: str
    candidates: tuple[ReuseCandidate, ...]

    def search(self, capability_id: str) -> tuple[ReuseCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.capability_id == capability_id)
