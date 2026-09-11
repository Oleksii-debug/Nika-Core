from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
)
from nika_core.research.knowledge_schema import _backfill_legacy_corpus, _rebuild_current_fts
from nika_core.research.retrieval_authorization import (
    RetrievalAuthorizationBinding,
    StandingPermissionKnowledgeRetriever,
)
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk

_START = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
_CONTEXT = PermissionContext(
    user_id="user-1",
    project_id="project-1",
    task_id="task-1",
)
_BINDING = RetrievalAuthorizationBinding(
    permission_id="perm-research",
    subject_id="agent-1",
    principal_id="agent-1",
    context=_CONTEXT,
)


def _setup(
    tmp_path: Path,
) -> tuple[
    SQLiteStore,
    KnowledgeCorpus,
    StandingPermissionStore,
    StandingPermissionKnowledgeRetriever,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("ws", "Research", _START.isoformat(), _START.isoformat()),
        )
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, 'local_file', ?, ?, ?)""",
            ("native-source", "ws", "approved:native", _START.isoformat(), _START.isoformat()),
        )
    corpus = KnowledgeCorpus(store)
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    retriever = StandingPermissionKnowledgeRetriever(
        store=store,
        corpus=corpus,
        permissions=permissions,
    )
    return store, corpus, permissions, retriever


def _ingest(
    store: SQLiteStore,
    corpus: KnowledgeCorpus,
    *,
    document_id: str,
    title: str,
    text: str,
    legacy: bool = True,
) -> None:
    if legacy:
        with store.connection() as conn:
            conn.execute(
                """INSERT INTO corpus_documents(
                    document_id, workspace_id, normalized_sha256, title, media_type,
                    normalized_text, created_at
                ) VALUES (?, 'ws', ?, ?, 'text/plain', ?, ?)""",
                (
                    document_id,
                    hashlib.sha256(text.encode()).hexdigest(),
                    title,
                    text,
                    _START.isoformat(),
                ),
            )
            _backfill_legacy_corpus(conn)
            _rebuild_current_fts(conn)
        return
    corpus.ingest(
        KnowledgeIngestRequest(
            workspace_id="ws",
            artifact_key=document_id,
            title=title,
            media_type="text/plain",
            text=text,
            source_locator="approved:native",
            parser_name="text",
            parser_version="1",
            approved_by="approval:owner",
            source_id="native-source",
        )
    )


def _grant(permissions: StandingPermissionStore, *document_ids: str) -> None:
    permissions.grant(
        permission_id=_BINDING.permission_id,
        scope=StandingPermissionScope(
            subject_id=_BINDING.subject_id,
            context=_CONTEXT,
            action_class="research.search",
            targets=("research-workspace:ws",),
            sites=(),
            resources=tuple(f"research-document:{item}" for item in document_ids),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=_START,
            expires_at=_START + timedelta(hours=1),
        ),
    )


def test_denied_high_rank_document_cannot_consume_limit_or_materialize(
    tmp_path: Path,
) -> None:
    store, corpus, permissions, retriever = _setup(tmp_path)
    _ingest(
        store,
        corpus,
        document_id="denied",
        title="needle needle needle",
        text=" ".join(["needle"] * 40 + ["DENIED_CANARY"]),
    )
    _ingest(
        store,
        corpus,
        document_id="allowed",
        title="Allowed",
        text="needle with deliberately longer lower relevance filler filler filler filler",
    )
    baseline = corpus.search(
        RetrievalScope(principal_id="agent-1", workspace_ids=("ws",)),
        "needle",
        limit=1,
    )
    assert baseline[0].provenance.artifact_key == "legacy:denied"
    _grant(permissions, "allowed")

    hits = retriever.search(
        binding=_BINDING,
        workspace_id="ws",
        query="needle",
        limit=1,
        now=_START + timedelta(minutes=1),
    )

    assert [hit.provenance.artifact_key for hit in hits] == ["legacy:allowed"]
    assert all("DENIED_CANARY" not in hit.text for hit in hits)
    assert all("DENIED_CANARY" not in hit.snippet for hit in hits)


def test_revocation_is_observed_by_the_next_search(tmp_path: Path) -> None:
    store, corpus, permissions, retriever = _setup(tmp_path)
    _ingest(
        store,
        corpus,
        document_id="revocable",
        title="Revocable",
        text="needle revocable content",
    )
    _grant(permissions, "revocable")

    first = retriever.search(
        binding=_BINDING,
        workspace_id="ws",
        query="needle",
        now=_START + timedelta(minutes=1),
    )
    assert [hit.provenance.artifact_key for hit in first] == ["legacy:revocable"]

    permissions.revoke(
        _BINDING.permission_id,
        revoked_at=_START + timedelta(minutes=2),
    )
    second = retriever.search(
        binding=_BINDING,
        workspace_id="ws",
        query="needle",
        now=_START + timedelta(minutes=3),
    )

    assert second == []


def test_unmapped_native_artifact_fails_closed(tmp_path: Path) -> None:
    store, corpus, permissions, retriever = _setup(tmp_path)
    _ingest(
        store,
        corpus,
        document_id="native-document",
        title="Native",
        text="needle native canary",
        legacy=False,
    )
    _grant(permissions, "native-document")

    hits = retriever.search(
        binding=_BINDING,
        workspace_id="ws",
        query="needle",
        now=_START + timedelta(minutes=1),
    )

    assert hits == []