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

CandidateIdentity = tuple[str, frozenset[str], tuple[tuple[str, str], ...]]


@runtime_checkable
class ReuseMetadataSource(Protocol):
    source_id: str

    def search(self, capability_id: str) -> tuple[ReuseCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class ReuseSearchResult:
    candidates: tuple[ReuseCandidate, ...]
    attempted_sources: tuple[str, ...]


def _candidate_identity(candidate: ReuseCandidate) -> CandidateIdentity:
    return candidate.digest, candidate.permissions, tuple(sorted(candidate.metadata.items()))


class ReuseSearchPipeline:
    """Deterministically search capability metadata in the binding reuse order.

    Sources return metadata only. This layer never imports, installs, downloads, executes, or
    trusts candidate code. Permission widening is filtered before candidates can reach selection.
    A single source may repeat an identical candidate, but it must not equivocate about immutable
    identity for the same capability version.
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
        seen: dict[tuple[str, str, str], CandidateIdentity] = {}
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
                key = (source_id, candidate.capability_id, candidate.version)
                identity = _candidate_identity(candidate)
                prior = seen.get(key)
                if prior is not None:
                    if prior != identity:
                        raise ValueError(
                            "reuse metadata source "
                            f"{source_id} returned conflicting identity for "
                            f"{candidate.capability_id} version {candidate.version}"
                        )
                    continue
                seen[key] = identity
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
