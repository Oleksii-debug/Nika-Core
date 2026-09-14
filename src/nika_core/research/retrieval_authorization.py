from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    CorpusCorruptionError,
    KnowledgeCorpus,
    KnowledgeHit,
    RetrievalScope,
)
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

_ACTION_CLASS = "research.search"
_LEGACY_ARTIFACT_PREFIX = "legacy:"
_RESEARCH_DOCUMENT_PREFIX = "research-document:"


@dataclass(frozen=True, slots=True)
class RetrievalAuthorizationBinding:
    """Trusted host identities used to derive one search's current authority."""

    permission_id: str
    subject_id: str
    principal_id: str
    context: PermissionContext

    def __post_init__(self) -> None:
        for name, value in (
            ("permission_id", self.permission_id),
            ("subject_id", self.subject_id),
            ("principal_id", self.principal_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")


class StandingPermissionKnowledgeRetriever:
    """Compose current standing authority with the incumbent KnowledgeCorpus boundary."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        corpus: KnowledgeCorpus,
        permissions: StandingPermissionStore,
    ) -> None:
        self._store = store
        self._corpus = corpus
        self._permissions = permissions

    def search(
        self,
        *,
        binding: RetrievalAuthorizationBinding,
        workspace_id: str,
        query: str,
        limit: int = 20,
        now: datetime | None = None,
    ) -> list[KnowledgeHit]:
        """Authorize exact artifacts first, then rank/materialize only the authorized set."""
        allowed_artifact_keys = self._authorized_legacy_artifact_keys(
            binding=binding,
            workspace_id=workspace_id,
            now=now,
        )
        return self._corpus.search(
            RetrievalScope(
                principal_id=binding.principal_id,
                workspace_ids=(workspace_id,),
                allowed_artifact_keys=allowed_artifact_keys,
            ),
            query,
            limit=limit,
        )

    def _authorized_legacy_artifact_keys(
        self,
        *,
        binding: RetrievalAuthorizationBinding,
        workspace_id: str,
        now: datetime | None,
    ) -> tuple[str, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT artifact_key FROM knowledge_artifacts
                WHERE workspace_id=? ORDER BY artifact_key""",
                (workspace_id,),
            ).fetchall()

        authorized: list[str] = []
        target = f"research-workspace:{workspace_id}"
        for row in rows:
            artifact_key = str(row["artifact_key"])
            resource_id = self._legacy_resource_id(artifact_key)
            if resource_id is None:
                continue
            document_id = artifact_key.removeprefix(_LEGACY_ARTIFACT_PREFIX)
            use = StandingPermissionUse(
                subject_id=binding.subject_id,
                context=binding.context,
                intent=ActionIntent(
                    action_id=f"research-search:{workspace_id}:{document_id}",
                    tool_id=_ACTION_CLASS,
                    risk=ToolRisk.READ_ONLY,
                    target=target,
                ),
                resource_id=resource_id,
            )
            try:
                self._permissions.authorize(
                    binding.permission_id,
                    use,
                    now=now,
                )
            except PermissionError:
                continue
            authorized.append(artifact_key)
        return tuple(authorized)

    @staticmethod
    def _legacy_resource_id(artifact_key: str) -> str | None:
        if not artifact_key.startswith(_LEGACY_ARTIFACT_PREFIX):
            return None
        document_id = artifact_key.removeprefix(_LEGACY_ARTIFACT_PREFIX)
        if not document_id:
            raise CorpusCorruptionError("legacy knowledge artifact is missing document identity")
        return f"{_RESEARCH_DOCUMENT_PREFIX}{document_id}"
