from __future__ import annotations

import pytest

from nika_core.toolsmith.contracts import CapabilityGap, GapKind, ReuseCandidate
from nika_core.toolsmith.reuse_search import ReuseSearchPipeline, StaticReuseMetadataSource


def _gap(*, permission_ceiling: frozenset[str] | None = None) -> CapabilityGap:
    return CapabilityGap(
        task_id="task-reuse-identity",
        requested_capability="tool.example",
        kind=GapKind.MISSING_CAPABILITY,
        reason="required capability is unavailable",
        attempted_methods=("tool_registry",),
        permission_ceiling=permission_ceiling or frozenset({"fs.read", "tests.run"}),
    )


def _candidate(
    *,
    source: str = "tool_registry",
    version: str = "1.2.3",
    digest: str = "sha256:artifact-a",
    permissions: frozenset[str] = frozenset({"fs.read"}),
    metadata: dict[str, str] | None = None,
) -> ReuseCandidate:
    return ReuseCandidate(
        capability_id="tool.example",
        version=version,
        source=source,
        digest=digest,
        permissions=permissions,
        metadata={} if metadata is None else metadata,
    )


def test_identical_repeat_from_one_source_is_deduplicated() -> None:
    candidate = _candidate(metadata={"origin": "registry-entry-7"})
    source = StaticReuseMetadataSource("tool_registry", (candidate, candidate))

    result = ReuseSearchPipeline((source,)).search(_gap())

    assert result.candidates == (candidate,)
    assert result.attempted_sources == ("tool_registry",)


def test_same_source_version_with_conflicting_digest_fails_closed() -> None:
    source = StaticReuseMetadataSource(
        "tool_registry",
        (
            _candidate(digest="sha256:artifact-a"),
            _candidate(digest="sha256:artifact-b"),
        ),
    )

    with pytest.raises(ValueError, match="conflicting identity"):
        ReuseSearchPipeline((source,)).search(_gap())


def test_conflict_is_detected_before_permission_filtering() -> None:
    source = StaticReuseMetadataSource(
        "tool_registry",
        (
            _candidate(permissions=frozenset({"fs.read"})),
            _candidate(permissions=frozenset({"fs.read", "network.any"})),
        ),
    )

    with pytest.raises(ValueError, match="conflicting identity"):
        ReuseSearchPipeline((source,)).search(_gap(permission_ceiling=frozenset({"fs.read"})))


def test_same_source_version_with_conflicting_provenance_metadata_fails_closed() -> None:
    source = StaticReuseMetadataSource(
        "tool_registry",
        (
            _candidate(metadata={"source_uri": "catalog:item-a"}),
            _candidate(metadata={"source_uri": "catalog:item-b"}),
        ),
    )

    with pytest.raises(ValueError, match="conflicting identity"):
        ReuseSearchPipeline((source,)).search(_gap())


def test_distinct_sources_keep_separate_identity_authority_and_binding_order() -> None:
    catalog = _candidate(
        source="approved_catalog",
        digest="sha256:catalog-artifact",
    )
    registry = _candidate(
        source="tool_registry",
        digest="sha256:registry-artifact",
    )
    pipeline = ReuseSearchPipeline(
        (
            StaticReuseMetadataSource("approved_catalog", (catalog,)),
            StaticReuseMetadataSource("tool_registry", (registry,)),
        )
    )

    result = pipeline.search(_gap())

    assert result.candidates == (registry, catalog)
    assert result.attempted_sources == ("tool_registry", "approved_catalog")


def test_same_source_can_publish_distinct_versions() -> None:
    first = _candidate(version="1.2.3", digest="sha256:first")
    second = _candidate(version="1.2.4", digest="sha256:second")
    source = StaticReuseMetadataSource("tool_registry", (first, second))

    result = ReuseSearchPipeline((source,)).search(_gap())

    assert result.candidates == (first, second)
