from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_REQUIRED_MODULES = (
    "nika_core.research.source_identity",
    "nika_core.research.knowledge",
    "nika_core.research_product_handoff",
    "nika_core.product_decisions",
)
_PUBLIC_IP = "93.184.216.34"


def _missing_modules() -> tuple[str, ...]:
    missing: list[str] = []
    for name in _REQUIRED_MODULES:
        try:
            available = find_spec(name) is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            missing.append(name)
    return tuple(missing)


_MISSING_MODULES = _missing_modules()
_requires_convergence = pytest.mark.skipif(
    bool(_MISSING_MODULES),
    reason="ONE-SHOT-38 prerequisite production slices are not integrated on this main",
)


def _components() -> SimpleNamespace:
    return SimpleNamespace(
        data=import_module("nika_core.data.sqlite"),
        product=import_module("nika_core.product_project"),
        decisions=import_module("nika_core.product_decisions"),
        research=import_module("nika_core.research"),
        handoff=import_module("nika_core.research_product_handoff"),
        knowledge=import_module("nika_core.research.knowledge"),
    )


def _environment(tmp_path: Path) -> SimpleNamespace:
    c = _components()
    store = c.data.SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = c.research.ResearchRepository(store)
    network = c.research.NetworkResearchRepository(store)
    repository.upsert_workspace(c.research.ResearchWorkspace("ws-a", "Research A"))
    repository.upsert_workspace(c.research.ResearchWorkspace("ws-b", "Research B"))

    response_state = {
        "body": b"keyboard operation needs deterministic semantic focus evidence",
        "etag": '"v1"',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={
                "Content-Type": "text/plain",
                "ETag": str(response_state["etag"]),
            },
            content=bytes(response_state["body"]),
        )

    fetcher = c.research.HttpxResearchFetcher(
        resolver=lambda _host, _port: (_PUBLIC_IP,),
        transport=httpx.MockTransport(handler),
    )
    research_service = c.research.HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=c.research.ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=fetcher,
        sleeper=lambda _: None,
    )
    research_service.register_source(
        c.research.SourceSpec(
            "source-a",
            "ws-a",
            c.research.SourceKind.HTTP,
            "https://example.com/research#ignored-fragment",
        )
    )
    refreshed = research_service.refresh_source("source-a")
    assert refreshed.disposition is c.research.RefreshDisposition.CHANGED

    result_set = c.research.ResearchResultService(
        repository=repository,
        network_repository=network,
    ).search("ws-a", "deterministic semantic focus")
    assert result_set.items
    assert result_set.items[0].evidence

    projects = c.product.ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Accessible evidence-backed product",
        spec=c.product.ProductProjectSpec(
            goal="Build an accessible product",
            desired_outcome="Requirements preserve research provenance",
            requirements=(
                c.product.ProductRequirement(
                    "req-1",
                    "Keyboard operation uses deterministic semantic focus",
                    ("Primary actions remain keyboard reachable",),
                ),
            ),
        ),
        idempotency_key="create:p1",
    )
    return SimpleNamespace(
        c=c,
        store=store,
        repository=repository,
        network=network,
        research_service=research_service,
        response_state=response_state,
        result_set=result_set,
        projects=projects,
    )


def _evidence_package(env: SimpleNamespace):
    c = env.c
    item = env.result_set.items[0]
    evidence = tuple(
        c.product.EvidenceRef(
            evidence_id=f"rs-item-{item.ordinal}-evidence-{index}",
            provenance_ref=(
                f"research-result-set://{env.result_set.workspace_id}/"
                f"{env.result_set.result_set_id}/items/{item.ordinal}/evidence/{index}"
            ),
            claim=item.snippet,
        )
        for index, _ in enumerate(item.evidence)
    )
    return c.product.ResearchEvidencePackage(
        package_id="research-evidence-1",
        evidence=evidence,
        research_artifact_ref=(
            f"research-result-set://{env.result_set.workspace_id}/"
            f"{env.result_set.result_set_id}"
        ),
    )


def _option(env: SimpleNamespace):
    return env.c.product.ProductOption(
        option_id="option-1",
        title="Semantic keyboard path",
        summary="Use deterministic semantic focus backed by the research result.",
        evidence_package_ids=("research-evidence-1",),
    )


def _approved_decision(env: SimpleNamespace):
    return env.c.product.ProductDecision(
        decision_id="decision-1",
        option_id="option-1",
        state=env.c.product.ProductDecisionState.APPROVED,
        rationale="The option is supported by the sealed research evidence.",
        decided_by_ref="user://owner",
    )


def _formal_handoff(env: SimpleNamespace):
    service = env.c.handoff.ResearchProductHandoffService(
        store=env.store,
        network_repository=env.network,
    )
    record = service.handoff(
        project_id="p1",
        result_set_id=env.result_set.result_set_id,
        package_id="research-evidence-1",
        options=(_option(env),),
    )
    return service, record


def test_one_shot_38_prerequisites_are_integrated_before_acceptance_credit() -> None:
    assert not _MISSING_MODULES, (
        "ONE-SHOT-38 remains QA-only until production prerequisites are on main; "
        f"missing modules: {_MISSING_MODULES}"
    )


@_requires_convergence
def test_research_corpus_product_lineage_survives_restart(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    c = env.c
    package = _evidence_package(env)
    item = env.result_set.items[0]
    source = env.network.get_source("source-a")
    assert source.current_raw_sha256 is not None

    corpus = c.knowledge.KnowledgeCorpus(env.store)
    version = corpus.ingest(
        c.knowledge.KnowledgeIngestRequest(
            workspace_id="ws-a",
            artifact_key="research-result-1",
            title=item.title,
            media_type="text/plain",
            text=item.snippet,
            source_id="source-a",
            source_locator="https://example.com/research",
            raw_sha256=source.current_raw_sha256,
            parser_name="research-result-set",
            parser_version="1",
            approved_by="approval:owner",
            visibility=c.knowledge.KnowledgeVisibility.RESTRICTED,
            allowed_principals=("user:reader",),
        )
    )
    assert version.version == 1

    reader_scope = c.knowledge.RetrievalScope("user:reader", ("ws-a",))
    denied_scope = c.knowledge.RetrievalScope("user:denied", ("ws-a",))
    hits = corpus.search(reader_scope, "deterministic semantic focus")
    assert hits
    assert corpus.search(denied_scope, "deterministic semantic focus") == []
    assert hits[0].provenance.source_id == "source-a"
    assert hits[0].provenance.raw_sha256 == source.current_raw_sha256

    handoff_service, handoff_record = _formal_handoff(env)
    assert handoff_record.package_id == package.package_id
    assert handoff_record.result_set_id == env.result_set.result_set_id

    decisions = c.decisions.ProductDecisionRepository(env.store)
    stored = decisions.record(
        "p1",
        _approved_decision(env),
        expected_row_version=0,
        idempotency_key="decision:approve",
    )
    linked = decisions.link_requirement(
        "p1",
        requirement_id="req-1",
        decision_id=stored.decision.decision_id,
        expected_row_version=1,
    )
    requirement = linked.spec.requirements[0]
    assert requirement.decision_ids == ("decision-1",)
    assert requirement.evidence_package_ids == (package.package_id,)

    restarted_store = c.data.SQLiteStore(env.store.path)
    restarted_store.initialize()
    restarted_network = c.research.NetworkResearchRepository(restarted_store)
    restarted_corpus = c.knowledge.KnowledgeCorpus(restarted_store)
    restarted_hits = restarted_corpus.search(reader_scope, "deterministic semantic focus")
    assert restarted_hits
    assert restarted_hits[0].provenance.raw_sha256 == source.current_raw_sha256

    restarted_handoff = c.handoff.ResearchProductHandoffService(
        store=restarted_store,
        network_repository=restarted_network,
    ).get("p1", package.package_id)
    assert restarted_handoff.result_set_sha256 == handoff_record.result_set_sha256
    recovered = c.product.ProductProjectRepository(restarted_store).get("p1")
    recovered_requirement = recovered.spec.requirements[0]
    assert recovered_requirement.decision_ids == requirement.decision_ids
    assert recovered_requirement.evidence_package_ids == requirement.evidence_package_ids
    assert handoff_service.get("p1", package.package_id) == handoff_record


@_requires_convergence
def test_source_content_update_invalidates_old_result_and_decision_replay(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    c = env.c
    handoff_service, _ = _formal_handoff(env)
    decisions = c.decisions.ProductDecisionRepository(env.store)
    decision = _approved_decision(env)
    decisions.record(
        "p1",
        decision,
        expected_row_version=0,
        idempotency_key="decision:approve",
    )
    original_sha = env.network.get_source("source-a").current_raw_sha256

    env.response_state["body"] = b"changed source bytes with replacement research evidence"
    env.response_state["etag"] = '"v2"'
    changed = env.research_service.refresh_source("source-a")
    assert changed.disposition is c.research.RefreshDisposition.CHANGED
    current_sha = env.network.get_source("source-a").current_raw_sha256
    assert current_sha is not None
    assert current_sha != original_sha

    with pytest.raises(c.product.ProductProjectError, match="source|content|stale|research"):
        handoff_service.get("p1", "research-evidence-1")
    with pytest.raises(c.product.ProductProjectError, match="source|content|stale|research"):
        decisions.record(
            "p1",
            decision,
            expected_row_version=0,
            idempotency_key="decision:approve",
        )


@_requires_convergence
def test_cross_workspace_source_substitution_is_rejected_by_corpus(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    c = env.c
    env.network.register_source(
        c.research.SourceSpec(
            "source-b",
            "ws-b",
            c.research.SourceKind.HTTP,
            "https://example.com/workspace-b",
        )
    )
    corpus = c.knowledge.KnowledgeCorpus(env.store)
    with pytest.raises(PermissionError, match="workspace"):
        corpus.ingest(
            c.knowledge.KnowledgeIngestRequest(
                workspace_id="ws-a",
                artifact_key="forged-cross-workspace",
                title="Forged",
                media_type="text/plain",
                text="forged cross workspace evidence",
                source_id="source-b",
                source_locator="https://example.com/workspace-b",
                parser_name="research-result-set",
                parser_version="1",
                approved_by="approval:owner",
            )
        )


@_requires_convergence
def test_corrupt_corpus_provenance_fails_closed(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    c = env.c
    item = env.result_set.items[0]
    source = env.network.get_source("source-a")
    corpus = c.knowledge.KnowledgeCorpus(env.store)
    corpus.ingest(
        c.knowledge.KnowledgeIngestRequest(
            workspace_id="ws-a",
            artifact_key="corruption-target",
            title=item.title,
            media_type="text/plain",
            text=item.snippet,
            source_id="source-a",
            source_locator="https://example.com/research",
            raw_sha256=source.current_raw_sha256,
            parser_name="research-result-set",
            parser_version="1",
            approved_by="approval:owner",
        )
    )
    with env.store.connection() as conn:
        conn.execute(
            "UPDATE knowledge_versions SET source_locator='https://attacker.example/forged' "
            "WHERE workspace_id='ws-a' AND artifact_key='corruption-target' AND version=1"
        )

    with pytest.raises(c.knowledge.CorpusCorruptionError):
        corpus.verify_integrity()


@_requires_convergence
def test_formal_handoff_cannot_be_downgraded_before_decision(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    c = env.c
    _formal_handoff(env)
    with env.store.connection() as conn:
        conn.execute(
            "DELETE FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' "
            "AND operation_kind='research_product_handoff.formal_authority'"
        )

    decisions = c.decisions.ProductDecisionRepository(env.store)
    with pytest.raises(c.product.ProductProjectError, match="formal research handoff authority"):
        decisions.record(
            "p1",
            _approved_decision(env),
            expected_row_version=0,
            idempotency_key="decision:downgrade-attack",
        )
