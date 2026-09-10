from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ExtractedDocument, ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query import DeterministicResearchQueryService, ResearchQuerySpec
from nika_core.research.repository import ResearchRepository


def _services(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    return store, repository, network


def _ingest_large_fixture(repository: ResearchRepository, *, count: int = 137) -> None:
    for index in reversed(range(count)):
        source = SourceSpec(
            source_id=f"source-{index:03d}",
            workspace_id="ws",
            kind=SourceKind.LOCAL_FILE,
            locator=f"fixture-{index:03d}.txt",
        )
        repository.upsert_source(source)
        repository.ingest_document(
            source,
            ExtractedDocument(
                title=f"document-{index:03d}",
                text=f"bounded alpha marker {index:03d}",
                media_type="text/plain",
            ),
        )


def test_large_query_is_bounded_stable_and_reports_truncation(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    _ingest_large_fixture(repository)
    service = DeterministicResearchQueryService(store=store, network_repository=network)
    spec = ResearchQuerySpec(workspace_id="ws", text="bounded alpha", limit=25)

    first = service.execute(spec)
    second = service.execute(spec)

    assert len(first.result_set.items) == 25
    assert first.bounds.requested_limit == 25
    assert first.bounds.returned_count == 25
    assert first.bounds.truncated is True
    assert [(item.rank, item.document_id) for item in first.result_set.items] == sorted(
        (item.rank, item.document_id) for item in first.result_set.items
    )
    assert [item.document_id for item in second.result_set.items] == [
        item.document_id for item in first.result_set.items
    ]
    rendered = service.render_text(first)
    assert "Limit: 25" in rendered
    assert "Results: 25" in rendered
    assert "Truncated: yes" in rendered


def test_maximum_allowed_limit_remains_bounded(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    _ingest_large_fixture(repository)
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    execution = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="bounded alpha", limit=100)
    )

    assert len(execution.result_set.items) == 100
    assert execution.bounds.requested_limit == 100
    assert execution.bounds.returned_count == 100
    assert execution.bounds.truncated is True


@pytest.mark.parametrize("limit", [0, -1, 101, 10**9])
def test_out_of_contract_limits_fail_closed(tmp_path, limit: int) -> None:
    store, _, network = _services(tmp_path)
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        service.execute(ResearchQuerySpec(workspace_id="ws", text="bounded", limit=limit))


@pytest.mark.parametrize("limit", [True, False, 1.5, "20"])
def test_non_integer_limits_fail_closed(tmp_path, limit: object) -> None:
    store, _, network = _services(tmp_path)
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    with pytest.raises(TypeError, match="limit must be an integer"):
        service.execute(ResearchQuerySpec(workspace_id="ws", text="bounded", limit=limit))  # type: ignore[arg-type]
