from __future__ import annotations

from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research.models import (
    FreshnessState,
    RefreshDisposition,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.repository import ResearchRepository


def _network(tmp_path: Path) -> tuple[SQLiteStore, NetworkResearchRepository, str, str]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    network.register_source(
        SourceSpec("source", "ws", SourceKind.HTTP, "https://example.com/source")
    )
    tasks = TaskQueue(store)
    task_a = tasks.create(workspace_id="ws", agent_id="research-test").task_id
    task_b = tasks.create(workspace_id="ws", agent_id="research-test").task_id
    return store, network, task_a, task_b


def _record_and_finalize(
    network: NetworkResearchRepository,
    *,
    task_id: str,
    attempt_number: int = 1,
    disposition: RefreshDisposition = RefreshDisposition.CHANGED,
) -> None:
    network.record_attempt(
        source_id="source",
        attempt_number=attempt_number,
        disposition=disposition,
        requested_url="https://example.com/source",
        final_url="https://example.com/source",
        status_code=200,
        error_code=None,
        error_message="",
        retryable=False,
        task_id=task_id,
    )
    network.finalize_source(
        "source",
        disposition=disposition,
        final_url="https://example.com/source",
        status_code=200,
        current_raw_sha256="a" * 64,
    )


def test_task_bound_recovery_cannot_borrow_another_tasks_finalization(tmp_path: Path) -> None:
    store, network, task_a, task_b = _network(tmp_path)
    baseline = network.task_attempt_count(task_id=task_a, source_id="source")

    _record_and_finalize(network, task_id=task_b)

    assert network.durable_task_result_after(
        task_id=task_a,
        source_id="source",
        attempts_before=baseline,
    ) is None

    _record_and_finalize(network, task_id=task_a)
    recovered = network.durable_task_result_after(
        task_id=task_a,
        source_id="source",
        attempts_before=baseline,
    )
    assert recovered is not None
    assert recovered.disposition is RefreshDisposition.CHANGED
    assert recovered.attempts == 1
    assert network.get_source("source").freshness is FreshnessState.CURRENT

    with store.connection() as conn:
        row = conn.execute(
            """SELECT attempt_id FROM research_http_attempts
            WHERE task_id=? AND source_id=? ORDER BY observed_at, rowid""",
            (task_a, "source"),
        ).fetchall()[-1]
    assert str(row["attempt_id"]).startswith("finalized:")


def test_unfinalized_attempt_fails_closed_as_restart_evidence(tmp_path: Path) -> None:
    _, network, task_a, _ = _network(tmp_path)
    baseline = network.task_attempt_count(task_id=task_a, source_id="source")
    network.record_attempt(
        source_id="source",
        attempt_number=1,
        disposition=RefreshDisposition.FAILED,
        requested_url="https://example.com/source",
        final_url="https://example.com/source",
        status_code=503,
        error_code="http_503",
        error_message="retry later",
        retryable=True,
        task_id=task_a,
    )

    assert network.durable_task_result_after(
        task_id=task_a,
        source_id="source",
        attempts_before=baseline,
    ) is None


def test_latest_post_baseline_attempt_must_be_atomically_finalized(tmp_path: Path) -> None:
    _, network, task_a, _ = _network(tmp_path)
    _record_and_finalize(network, task_id=task_a)
    baseline = 0
    network.record_attempt(
        source_id="source",
        attempt_number=2,
        disposition=RefreshDisposition.FAILED,
        requested_url="https://example.com/source",
        final_url="https://example.com/source",
        status_code=503,
        error_code="http_503",
        error_message="interrupted retry",
        retryable=True,
        task_id=task_a,
    )

    assert network.durable_task_result_after(
        task_id=task_a,
        source_id="source",
        attempts_before=baseline,
    ) is None
