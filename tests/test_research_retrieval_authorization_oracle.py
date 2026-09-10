from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ExtractedDocument, ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query import DeterministicResearchQueryService, ResearchQuerySpec
from nika_core.research.repository import ResearchRepository
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

_START = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
_CONTEXT = PermissionContext(user_id="user-1", project_id="project-1", task_id="task-1")
_ACTION_CLASS = "research.search"
_TARGET = "research-workspace:ws"


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    network = NetworkResearchRepository(store)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    service = DeterministicResearchQueryService(store=store, network_repository=network)
    return repository, permissions, service


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


def _resource(document_id: str) -> str:
    return f"research-document:{document_id}"


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
            action_class=_ACTION_CLASS,
            targets=(_TARGET,),
            sites=(),
            resources=tuple(_resource(document_id) for document_id in document_ids),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=_START,
            expires_at=_START + timedelta(hours=1),
        ),
    )


def _use(document_id: str) -> StandingPermissionUse:
    resource_id = _resource(document_id)
    return StandingPermissionUse(
        subject_id="agent-1",
        context=_CONTEXT,
        intent=ActionIntent(
            action_id=f"research-search:{document_id}",
            tool_id=_ACTION_CLASS,
            risk=ToolRisk.READ_ONLY,
            target=_TARGET,
            task_id=_CONTEXT.task_id,
            project_id=_CONTEXT.project_id,
            resource=resource_id,
        ),
        resource_id=resource_id,
    )


def _search(service: DeterministicResearchQueryService, *, limit: int = 20):
    return service.execute(
        ResearchQuerySpec(workspace_id="ws", text="needle", limit=limit)
    ).result_set.items


def test_authorized_document_is_retrievable(tmp_path) -> None:
    repository, permissions, service = _setup(tmp_path)
    allowed = _ingest(repository, source_id="allowed", title="Allowed", text="needle alpha")
    _grant(permissions, allowed.document_id)

    permissions.authorize(
        "perm-research",
        _use(allowed.document_id),
        now=_START + timedelta(minutes=1),
    )
    items = _search(service)

    assert [item.document_id for item in items] == [allowed.document_id]


def test_unauthorized_higher_ranked_document_never_crosses_retrieval_boundary(tmp_path) -> None:
    repository, permissions, service = _setup(tmp_path)
    restricted = _ingest(
        repository,
        source_id="restricted",
        title="needle needle needle",
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

    permissions.authorize(
        "perm-research",
        _use(allowed.document_id),
        now=_START + timedelta(minutes=1),
    )
    with pytest.raises(PermissionError):
        permissions.authorize(
            "perm-research",
            _use(restricted.document_id),
            now=_START + timedelta(minutes=1),
        )

    items = _search(service, limit=1)
    assert [item.document_id for item in items] == [allowed.document_id]


def test_mixed_result_set_contains_only_currently_authorized_documents(tmp_path) -> None:
    repository, permissions, service = _setup(tmp_path)
    allowed_a = _ingest(repository, source_id="allowed-a", title="Allowed A", text="needle alpha")
    restricted = _ingest(
        repository,
        source_id="restricted",
        title="Restricted",
        text="needle secret-canary",
    )
    allowed_b = _ingest(repository, source_id="allowed-b", title="Allowed B", text="needle beta")
    _grant(permissions, allowed_a.document_id, allowed_b.document_id)

    for document in (allowed_a, allowed_b):
        permissions.authorize(
            "perm-research",
            _use(document.document_id),
            now=_START + timedelta(minutes=1),
        )
    with pytest.raises(PermissionError):
        permissions.authorize(
            "perm-research",
            _use(restricted.document_id),
            now=_START + timedelta(minutes=1),
        )

    items = _search(service)
    assert {item.document_id for item in items} == {
        allowed_a.document_id,
        allowed_b.document_id,
    }
    assert all("secret-canary" not in item.snippet for item in items)


def test_revocation_before_second_search_is_observed_at_retrieval_boundary(tmp_path) -> None:
    repository, permissions, service = _setup(tmp_path)
    allowed = _ingest(repository, source_id="allowed", title="Allowed", text="needle alpha")
    _grant(permissions, allowed.document_id)

    permissions.authorize(
        "perm-research",
        _use(allowed.document_id),
        now=_START + timedelta(minutes=1),
    )
    first = _search(service)
    assert [item.document_id for item in first] == [allowed.document_id]

    permissions.revoke("perm-research", revoked_at=_START + timedelta(minutes=2))
    with pytest.raises(PermissionError, match="revoked"):
        permissions.authorize(
            "perm-research",
            _use(allowed.document_id),
            now=_START + timedelta(minutes=3),
        )

    second = _search(service)
    assert second == ()
