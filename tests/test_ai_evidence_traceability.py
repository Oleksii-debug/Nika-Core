from __future__ import annotations

import json

import pytest

from nika_core.intelligence.evidence_trace import (
    ResearchEvidenceSelection,
    ResearchEvidenceTraceError,
    attach_research_assisted_provenance,
)
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)


def _similar_source_result_set() -> ResearchResultSet:
    observed_at = "2026-09-10T17:30:00Z"
    return ResearchResultSet(
        result_set_id="results-similar-sources",
        workspace_id="workspace-1",
        query="fixture query body must not enter provenance",
        created_at="2026-09-10T17:31:00Z",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id="document-a",
                title="Nearly identical title",
                snippet="same-looking evidence body with secret-alpha",
                rank=1.0,
                why_matched="same-looking match rationale",
                evidence=(
                    ResearchEvidence(
                        source_id="source-a",
                        source_kind=SourceKind.LOCAL_FILE,
                        locator="C:/private/source-a.txt",
                        observed_at=observed_at,
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
            ResearchResultItem(
                ordinal=1,
                document_id="document-b",
                title="Nearly identical title",
                snippet="same-looking evidence body with secret-beta",
                rank=1.0,
                why_matched="same-looking match rationale",
                evidence=(
                    ResearchEvidence(
                        source_id="source-b",
                        source_kind=SourceKind.LOCAL_FILE,
                        locator="C:/private/source-b.txt",
                        observed_at=observed_at,
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
    )


def _selection(
    *, source_id: str = "source-b", document_id: str = "document-b"
) -> ResearchEvidenceSelection:
    return ResearchEvidenceSelection(
        result_set_id="results-similar-sources",
        item_ordinal=1,
        document_id=document_id,
        evidence_index=0,
        source_id=source_id,
        observed_at="2026-09-10T17:30:00Z",
    )


def test_response_provenance_references_exact_selected_similar_source_without_body() -> None:
    result_set = _similar_source_result_set()

    response_payload = attach_research_assisted_provenance(
        output={"text": "research-assisted answer"},
        result_set=result_set,
        selections=(_selection(),),
    )
    provenance = response_payload["provenance"]
    assert isinstance(provenance, dict)
    traces = provenance["research_evidence"]
    assert traces == [
        {
            "schema": "nika.intelligence.research-evidence-trace:v1",
            "result_set_id": "results-similar-sources",
            "workspace_id": "workspace-1",
            "result_created_at": "2026-09-10T17:31:00Z",
            "item_ordinal": 1,
            "document_id": "document-b",
            "evidence_index": 0,
            "source_id": "source-b",
            "source_kind": "local_file",
            "observed_at": "2026-09-10T17:30:00Z",
            "freshness": "current",
        }
    ]

    rendered = json.dumps(response_payload, sort_keys=True)
    for unnecessary_source_body in (
        "fixture query body",
        "Nearly identical title",
        "same-looking evidence body",
        "same-looking match rationale",
        "C:/private/source-a.txt",
        "C:/private/source-b.txt",
        "secret-alpha",
        "secret-beta",
    ):
        assert unnecessary_source_body not in rendered


def test_fabricated_cross_source_association_fails_closed() -> None:
    result_set = _similar_source_result_set()

    with pytest.raises(
        ResearchEvidenceTraceError,
        match="selected source_id does not match retrieval evidence",
    ):
        attach_research_assisted_provenance(
            output={"text": "must not gain false provenance"},
            result_set=result_set,
            selections=(_selection(source_id="source-a"),),
        )


def test_same_looking_source_cannot_be_selected_by_wrong_document_identity() -> None:
    result_set = _similar_source_result_set()

    with pytest.raises(
        ResearchEvidenceTraceError,
        match="selected document_id does not match retrieval item",
    ):
        attach_research_assisted_provenance(
            output={"plan": ["step-1"]},
            result_set=result_set,
            selections=(_selection(document_id="document-a"),),
        )


def test_unvalidated_existing_provenance_is_not_silently_accepted() -> None:
    with pytest.raises(
        ResearchEvidenceTraceError,
        match="output already contains unvalidated provenance",
    ):
        attach_research_assisted_provenance(
            output={"text": "answer", "provenance": {"source_id": "fabricated"}},
            result_set=_similar_source_result_set(),
            selections=(_selection(),),
        )
