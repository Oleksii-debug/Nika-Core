from __future__ import annotations

from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research import (
    DeterministicResearchQueryService,
    ExtractedDocument,
    NetworkResearchRepository,
    RefreshDisposition,
    RefreshResult,
    ResearchProfile,
    ResearchProfileRepository,
    ResearchProfileRunService,
    ResearchRepository,
    ResearchSourceRef,
    ResearchSourceSet,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


class _FakeWeb:
    def __init__(self, dispositions: list[RefreshDisposition], tasks: TaskQueue | None = None) -> None:
        self.dispositions = list(dispositions)
        self.tasks = tasks
        self.calls: list[tuple[str, str | None]] = []
        self.pause_after_first = False

    def refresh_source(self, source_id: str, *, task_id: str | None = None) -> RefreshResult:
        self.calls.append((source_id, task_id))
        disposition = self.dispositions.pop(0)
        if self.pause_after_first and len(self.calls) == 1:
            assert self.tasks is not None
            assert task_id is not None
            self.tasks.transition(task_id, TaskState.PAUSED)
        return RefreshResult(source_id=source_id, disposition=disposition, attempts=1)


def _stack(tmp_path: Path, *, http_count: int = 1):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    corpus = ResearchRepository(store)
    corpus.upsert_workspace(ResearchWorkspace("ws", "Research"))
    local = SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/A.txt")
    corpus.upsert_source(local)
    corpus.ingest_document(
        local,
        ExtractedDocument(
            "Освітній грант",
            "Український освітній грант для студентів і викладачів.",
            "text/plain",
        ),
    )

    network = NetworkResearchRepository(store)
    http_ids: list[str] = []
    for index in range(http_count):
        source_id = f"http-{index + 1}"
        http_ids.append(source_id)
        network.register_source(
            SourceSpec(source_id, "ws", SourceKind.HTTP, f"https://example.org/{index + 1}")
        )

    profiles = ResearchProfileRepository(store)
    profiles.save_source_set(
        ResearchSourceSet(
            "grants",
            "ws",
            1,
            "Grant sources",
            (
                ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),
                *(ResearchSourceRef(source_id, SourceKind.HTTP) for source_id in http_ids),
            ),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            "education-grants",
            "ws",
            1,
            "Education grants",
            "grants",
            1,
            "освітній грант",
        )
    )
    tasks = TaskQueue(store)
    checkpoints = CheckpointService(store)
    query = DeterministicResearchQueryService(store=store, network_repository=network)
    return store, network, profiles, tasks, checkpoints, query, tuple(http_ids)


def _service(
    *,
    network: NetworkResearchRepository,
    profiles: ResearchProfileRepository,
    tasks: TaskQueue,
    checkpoints: CheckpointService,
    query: DeterministicResearchQueryService,
    web: _FakeWeb,
) -> ResearchProfileRunService:
    return ResearchProfileRunService(
        tasks=tasks,
        checkpoints=checkpoints,
        profiles=profiles,
        network_repository=network,
        query_service=query,
        web=web,  # type: ignore[arg-type]
    )


def test_profile_run_pins_versions_refreshes_http_and_persists_one_result(tmp_path: Path) -> None:
    store, network, profiles, tasks, checkpoints, query, http_ids = _stack(tmp_path)
    web = _FakeWeb([RefreshDisposition.NOT_MODIFIED], tasks)
    service = _service(
        network=network,
        profiles=profiles,
        tasks=tasks,
        checkpoints=checkpoints,
        query=query,
        web=web,
    )

    task_id = service.create_job("education-grants")
    task = tasks.get(task_id)
    assert task.payload["profile_version"] == 1
    assert task.payload["source_set_version"] == 1
    assert task.payload["http_source_ids"] == list(http_ids)

    summary = service.run(task_id)
    assert summary.state == "completed"
    assert summary.processed == 1
    assert summary.total == 1
    assert summary.unchanged == 1
    assert summary.changed == 0
    assert summary.failed == 0
    assert summary.result_set_id is not None
    assert summary.result_count == 1
    assert web.calls == [("http-1", task_id)]

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_result_sets").fetchone()[0] == 1

    repeated = service.run(task_id)
    assert repeated.result_set_id == summary.result_set_id
    assert web.calls == [("http-1", task_id)]
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_result_sets").fetchone()[0] == 1

    rendered = service.render_text(summary)
    assert "Profile: education-grants v1" in rendered
    assert "HTTP refresh: 1/1" in rendered
    assert "Results: 1" in rendered


def test_profile_run_resume_skips_completed_refresh_and_queries_once(tmp_path: Path) -> None:
    store, network, profiles, tasks, checkpoints, query, _ = _stack(tmp_path)
    web = _FakeWeb([RefreshDisposition.CHANGED], tasks)
    web.pause_after_first = True
    service = _service(
        network=network,
        profiles=profiles,
        tasks=tasks,
        checkpoints=checkpoints,
        query=query,
        web=web,
    )

    task_id = service.create_job("education-grants")
    paused = service.run(task_id)
    assert paused.state == "paused"
    assert paused.processed == 1
    assert paused.changed == 1
    assert paused.result_set_id is None

    resumed = service.resume(task_id)
    assert resumed.state == "completed"
    assert resumed.processed == 1
    assert resumed.changed == 1
    assert resumed.result_count == 1
    assert len(web.calls) == 1
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_result_sets").fetchone()[0] == 1


def test_profile_run_restart_reloads_exact_task_profile_and_result(tmp_path: Path) -> None:
    store, network, profiles, tasks, checkpoints, query, _ = _stack(tmp_path)
    web = _FakeWeb([RefreshDisposition.FAILED], tasks)
    service = _service(
        network=network,
        profiles=profiles,
        tasks=tasks,
        checkpoints=checkpoints,
        query=query,
        web=web,
    )
    task_id = service.create_job("education-grants")
    first = service.run(task_id)
    assert first.state == "completed"
    assert first.failed == 1
    assert first.result_count == 1

    profiles.save_profile(
        ResearchProfile(
            "education-grants",
            "ws",
            2,
            "Education grants v2",
            "grants",
            1,
            "term-that-does-not-match",
        )
    )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_network = NetworkResearchRepository(restarted_store)
    restarted_profiles = ResearchProfileRepository(restarted_store)
    restarted_tasks = TaskQueue(restarted_store)
    restarted_checkpoints = CheckpointService(restarted_store)
    restarted_query = DeterministicResearchQueryService(
        store=restarted_store,
        network_repository=restarted_network,
    )
    restarted_web = _FakeWeb([], restarted_tasks)
    restarted = _service(
        network=restarted_network,
        profiles=restarted_profiles,
        tasks=restarted_tasks,
        checkpoints=restarted_checkpoints,
        query=restarted_query,
        web=restarted_web,
    )

    again = restarted.run(task_id)
    assert again.profile_version == 1
    assert again.result_set_id == first.result_set_id
    assert again.result_count == 1
    assert restarted_web.calls == []


def test_query_result_identity_is_idempotent_for_crash_replay(tmp_path: Path) -> None:
    store, network, profiles, tasks, checkpoints, query, _ = _stack(tmp_path, http_count=0)
    web = _FakeWeb([], tasks)
    service = _service(
        network=network,
        profiles=profiles,
        tasks=tasks,
        checkpoints=checkpoints,
        query=query,
        web=web,
    )
    task_id = service.create_job("education-grants")
    stable_id = service._stable_result_set_id(task_id)
    profile = profiles.load_profile("education-grants", 1)
    source_set = profiles.load_source_set("grants", 1)
    spec = ResearchQuerySpec(
        workspace_id=profile.workspace_id,
        text=profile.query_text,
        mode=profile.query_mode,
        filters=ResearchSearchFilters(
            source_ids=tuple(source.source_id for source in source_set.sources),
        ),
        limit=profile.result_limit,
    )

    first = query.execute(spec, result_set_id=stable_id)
    second = query.execute(spec, result_set_id=stable_id)
    assert first.result_set == second.result_set
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_result_sets").fetchone()[0] == 1
