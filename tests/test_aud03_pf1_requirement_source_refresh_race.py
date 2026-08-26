from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import nika_core.product_decisions as product_decisions_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchResultService,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research_product_handoff import ResearchProductHandoffService

_PUBLIC_IP = "93.184.216.34"


def _environment(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    research = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    research.upsert_workspace(ResearchWorkspace("ws-a", "Research A"))
    response_state = {
        "body": b"keyboard operation needs deterministic semantic focus evidence",
        "etag": '"v1"',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain", "ETag": str(response_state["etag"])},
            content=bytes(response_state["body"]),
        )

    research_service = HttpResearchService(
        repository=research,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda _host, _port: (_PUBLIC_IP,),
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    research_service.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/research#ignored-fragment",
        )
    )
    first = research_service.refresh_source("source-a")
    assert first.disposition is RefreshDisposition.CHANGED

    result_set = ResearchResultService(
        repository=research,
        network_repository=network,
    ).search("ws-a", "deterministic semantic focus")
    assert result_set.items

    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Accessible evidence-backed product",
        spec=ProductProjectSpec(
            goal="Build an accessible product",
            desired_outcome="Requirements preserve exact research provenance",
            requirements=(
                ProductRequirement(
                    requirement_id="requirement-1",
                    text="Preserve the selected semantic interaction requirement.",
                    acceptance=("Evidence remains current at durable linkage.",),
                ),
            ),
        ),
        idempotency_key="create:p1",
    )
    handoff = ResearchProductHandoffService(store=store, network_repository=network)
    option = ProductOption(
        option_id="option-1",
        title="Semantic keyboard path",
        summary="Use deterministic semantic focus backed by research.",
        evidence_package_ids=("research-evidence-1",),
    )
    handoff.handoff(
        project_id="p1",
        result_set_id=result_set.result_set_id,
        package_id="research-evidence-1",
        options=(option,),
    )

    decisions = ProductDecisionRepository(store)
    decisions.record(
        "p1",
        ProductDecision(
            decision_id="decision-1",
            option_id="option-1",
            state=ProductDecisionState.APPROVED,
            rationale="The option is supported by sealed research evidence.",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:approve",
    )
    return store, network, research_service, response_state, decisions


def _requirement(project):
    return next(
        item for item in project.spec.requirements if item.requirement_id == "requirement-1"
    )


def test_requirement_link_succeeds_when_research_evidence_stays_current(
    tmp_path: Path,
) -> None:
    store, _network, _research_service, _response_state, decisions = _environment(tmp_path)

    linked = decisions.link_requirement(
        "p1",
        requirement_id="requirement-1",
        decision_id="decision-1",
        expected_row_version=1,
    )

    requirement = _requirement(linked)
    assert linked.row_version == 2
    assert requirement.decision_ids == ("decision-1",)
    assert requirement.evidence_package_ids == ("research-evidence-1",)
    assert ProductProjectRepository(store).get("p1") == linked


def test_source_refresh_after_verification_cannot_commit_stale_requirement_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, network, research_service, response_state, decisions = _environment(tmp_path)
    original_sha = network.get_source("source-a").current_raw_sha256
    assert original_sha is not None
    original_verify = product_decisions_module.verify_sealed_handoffs_conn
    refresh_injected = False

    def verify_then_refresh(conn, project_id: str, package_ids: tuple[str, ...]) -> None:
        nonlocal refresh_injected
        original_verify(conn, project_id, package_ids)
        if refresh_injected:
            return
        response_state["body"] = b"replacement research bytes after successful verification"
        response_state["etag"] = '"v2"'
        changed = research_service.refresh_source("source-a")
        assert changed.disposition is RefreshDisposition.CHANGED
        current_sha = network.get_source("source-a").current_raw_sha256
        assert current_sha is not None
        assert current_sha != original_sha
        refresh_injected = True

    monkeypatch.setattr(
        product_decisions_module,
        "verify_sealed_handoffs_conn",
        verify_then_refresh,
    )

    with pytest.raises(ProductProjectError):
        decisions.link_requirement(
            "p1",
            requirement_id="requirement-1",
            decision_id="decision-1",
            expected_row_version=1,
        )

    assert refresh_injected
    project = ProductProjectRepository(store).get("p1")
    requirement = _requirement(project)
    assert project.row_version == 1
    assert requirement.decision_ids == ()
    assert requirement.evidence_package_ids == ()
