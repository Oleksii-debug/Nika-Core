from __future__ import annotations

from nika_core.research.models import ResearchResultSet, SourceKind
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.repository import ResearchRepository


class ResearchResultService:
    def __init__(
        self,
        *,
        repository: ResearchRepository,
        network_repository: NetworkResearchRepository,
    ) -> None:
        self._repository = repository
        self._network = network_repository

    def search(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> ResearchResultSet:
        hits = self._repository.search(workspace_id, query, limit=limit)
        return self._network.save_result_set(
            workspace_id=workspace_id,
            query=query,
            hits=hits,
        )

    def get(self, result_set_id: str) -> ResearchResultSet:
        return self._network.get_result_set(result_set_id)

    @staticmethod
    def render_text(result_set: ResearchResultSet) -> str:
        lines = [
            "Research results",
            f"Query: {result_set.query}",
            f"Results: {len(result_set.items)}",
            "",
        ]
        for index, item in enumerate(result_set.items, start=1):
            lines.extend(
                [
                    f"{index}. {item.title}",
                    f"Why matched: {item.why_matched}",
                    f"Snippet: {item.snippet}",
                    "Sources:",
                ]
            )
            if not item.evidence:
                lines.append("- No source provenance recorded")
            for evidence in item.evidence:
                if evidence.source_kind is SourceKind.HTTP:
                    freshness = (
                        evidence.freshness.value
                        if evidence.freshness is not None
                        else "unknown"
                    )
                    label = f"HTTP, freshness={freshness}"
                else:
                    label = "Local file"
                lines.append(
                    f"- {label}: {evidence.locator} "
                    f"(observed {evidence.observed_at})"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
