from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ExtractedDocument, ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query import (
    RESEARCH_SEARCH_ACTION_CLASS,
    DeterministicResearchQueryService,
    ResearchQueryAuthorization,
    ResearchQuerySpec,
    research_document_resource,
    research_workspace_target,
)
from nika_core.research.repository import ResearchRepository
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk

_START = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
_CONTEXT = PermissionContext(user_id="user-1", project_id="project-1", task_id="task-1")


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    network = NetworkResearchRepository(store)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    return store, repository, network, permissions


def _ingest(repository: ResearchRepository, *, source_id: str, title: str, text: str):
    source = SourceSpec(
        source_id=source_id,
        workspace_id="ws",
        kind=SourceKind.LOCAL_FILE,
        locator=f"{source_id}.txt",
    )
    repository.upsert_source(source)
    return repository.ingest_document(
        source,
        ExtractedDocument(title=title, text=text, media_type="text/plain"),
    ).document


def _grant(
    permissions: StandingPermissionStore,
    *document_ids: str,
    permission_id: str = "perm-research",
) -> None:
    permissions.grant(
        permission_id=permission_id,
        scope=StandingPermissionScope(
            subject_id="agent-1",
            context=_CONTEXT,
            action_class=RESEARCH_SEARCH_ACTION_CLASS,
            targets=(research_workspace_target("ws"),),
            sites=(),
            resources=tuple(research_document_resource(item) for item in document_ids),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=_START,
            expires_at=_START + timedelta(hours=1),
        ),
    )


def _authorization(permission_id: str = "perm-research") -> ResearchQueryAuthorization:
    return ResearchQueryAuthorization(
        permission_id=permission_id,
        subject_id="agent-1",
        context=_CONTEXT,
    )


def _service(store, network, permissions, clock):
    return DeterministicResearchQueryService(
        store=store,
        network_repository=network,
        permission_store=permissions,
        clock=clock,
    )


def test_authorized_document_is_returned(tmp_path) -> None:
    store, repository, network, permissions = _setup(tmp_path)
    allowed = _ingest(repository, source_id="allowed", title="Allowed", text="needle alpha")
    _grant(permissions, allowed.document_id)
    service = _service(store, network, permissions, lambda: _START + timedelta(minutes=1))

    execution = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle"),
        authorization=_authorization(),
    )

    assert [item.document_id for item in execution.result_set.items] == [allowed.document_id]


def test_unauthorized_higher_ranked_document_cannot_consume_limit(tmp_path) -> None:
    store, repository, network, permissions = _setup(tmp_path)
    restricted = _ingest(
        repository,
        source_id="restricted",
        title="needle needle",
        text="needle needle needle needle needle",
    )
    allowed = _ingest(
        repository,
        source_id="allowed",
        title="Allowed",
        text="needle with deliberately longer lower relevance filler filler filler filler filler",
    )
    baseline = repository.search("ws", "needle", limit=2)
    assert baseline[0].document_id == restricted.document_id
    _grant(permissions, allowed.document_id)
    service = _service(store, network, permissions, lambda: _START + timedelta(minutes=1))

    execution = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle", limit=1),
        authorization=_authorization(),
    )

    assert [item.document_id for item in execution.result_set.items] == [allowed.document_id]
    rendered = service.render_text(execution)
    assert "needle needle needle needle needle" not in rendered
    assert restricted.document_id not in {item.document_id for item in execution.result_set.items}


def test_mixed_results_materialize_only_authorized_documents(tmp_path) -> None:
    store, repository, network, permissions = _setup(tmp_path)
    allowed_a = _ingest(repository, source_id="allowed-a", title="Allowed A", text="needle alpha")
    restricted = _ingest(
        repository,
        source_id="restricted",
        title="Restricted",
        text="needle secret-canary",
    )
    allowed_b = _ingest(repository, source_id="allowed-b", title="Allowed B", text="needle beta")
    _grant(permissions, allowed_a.document_id, allowed_b.document_id)
    service = _service(store, network, permissions, lambda: _START + timedelta(minutes=1))

    execution = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle", limit=10),
        authorization=_authorization(),
    )

    ids = {item.document_id for item in execution.result_set.items}
    assert ids == {allowed_a.document_id, allowed_b.document_id}
    assert restricted.document_id not in ids
    assert "secret-canary" not in service.render_text(execution)


def test_revocation_before_second_search_is_observed_without_restart(tmp_path) -> None:
    store, repository, network, permissions = _setup(tmp_path)
    allowed = _ingest(repository, source_id="allowed", title="Allowed", text="needle alpha")
    _grant(permissions, allowed.document_id)
    clock = [_START + timedelta(minutes=1)]
    service = _service(store, network, permissions, lambda: clock[0])

    first = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle"),
        authorization=_authorization(),
    )
    assert [item.document_id for item in first.result_set.items] == [allowed.document_id]

    permissions.revoke("perm-research", revoked_at=_START + timedelta(minutes=2))
    clock[0] = _START + timedelta(minutes=3)
    second = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle"),
        authorization=_authorization(),
    )

    assert second.result_set.items == ()


def test_configured_permission_boundary_rejects_missing_current_context(tmp_path) -> None:
    store, repository, network, permissions = _setup(tmp_path)
    _ingest(repository, source_id="allowed", title="Allowed", text="needle alpha")
    service = _service(store, network, permissions, lambda: _START + timedelta(minutes=1))

    with pytest.raises(PermissionError, match="current authorization context"):
        service.execute(ResearchQuerySpec(workspace_id="ws", text="needle"))
