from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.learning import StudyMaterial, StudyMaterialKind, StudyQueue


def _services(tmp_path):
    path = tmp_path / "Ніка навчання з пробілами.db"
    store = SQLiteStore(path)
    store.initialize()
    tasks = TaskQueue(store)
    return path, tasks, StudyQueue(tasks)


def _material(**overrides) -> StudyMaterial:
    values = {
        "material_id": "book-uk-001",
        "title": "Книга про причинне мислення",
        "kind": StudyMaterialKind.BOOK,
        "source_ref": r"C:\Мої книги\Причинне мислення 2026.pdf",
        "source_version": "edition-1",
        "content_sha256": "a" * 64,
        "learning_goal": "Виділити перевірювані твердження та суперечності.",
    }
    values.update(overrides)
    return StudyMaterial(**values)


def test_enqueue_survives_fresh_store_restart_with_unicode_path(tmp_path) -> None:
    path, _, queue = _services(tmp_path)

    created = queue.enqueue(
        workspace_id="особисте-навчання",
        agent_id="nika-reader",
        material=_material(),
    )

    assert created.state is TaskState.READY
    assert created.material.source_ref == r"C:\Мої книги\Причинне мислення 2026.pdf"

    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    fresh = StudyQueue(TaskQueue(fresh_store)).get(created.task_id)

    assert fresh == created


def test_study_task_uses_canonical_pause_resume_and_completion_states(tmp_path) -> None:
    _, _, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="research",
        agent_id="reader",
        material=_material(),
    )

    running = queue.start(task.task_id)
    paused = queue.pause(task.task_id)
    resumed = queue.resume(task.task_id)
    running_again = queue.start(task.task_id)
    completed = queue.complete(task.task_id)

    assert running.state is TaskState.RUNNING
    assert paused.state is TaskState.PAUSED
    assert resumed.state is TaskState.READY
    assert running_again.state is TaskState.RUNNING
    assert completed.state is TaskState.COMPLETED


def test_recent_study_listing_filters_other_tasks_workspace_and_agent(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    tasks.create(workspace_id="alpha", agent_id="other", payload={"kind": "ordinary"})
    first = queue.enqueue(
        workspace_id="alpha",
        agent_id="reader-a",
        material=_material(material_id="a"),
    )
    queue.enqueue(
        workspace_id="alpha",
        agent_id="reader-b",
        material=_material(material_id="b", title="Інший документ"),
    )
    queue.enqueue(
        workspace_id="beta",
        agent_id="reader-a",
        material=_material(material_id="c", title="Третій документ"),
    )

    selected = queue.list_recent(workspace_id="alpha", agent_id="reader-a")

    assert [item.task_id for item in selected] == [first.task_id]


def test_interrupted_enqueue_created_state_is_recovered_without_touching_other_tasks(
    tmp_path,
) -> None:
    _, tasks, queue = _services(tmp_path)
    interrupted = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="recover-me", title="Матеріал після збою"),
    )
    ordinary = tasks.create(
        workspace_id="study",
        agent_id="worker",
        payload={"kind": "ordinary"},
    )
    with tasks.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            (TaskState.CREATED.value, interrupted.task_id),
        )

    recovered = queue.recover_created()

    assert [item.task_id for item in recovered] == [interrupted.task_id]
    assert queue.get(interrupted.task_id).state is TaskState.READY
    assert tasks.get(ordinary.task_id).state is TaskState.CREATED


@pytest.mark.parametrize(
    "source_ref",
    [
        "https://example.test/book.pdf?api_key=secret",
        "https://user:secret@example.test/book.pdf",
        "https://example.test/book.pdf?%74oken=secret",
        "Authorization: Bearer secret",
        "https%3A%2F%2Fexample.test%2Fbook.pdf%3Ftoken%3Dsecret",
    ],
)
def test_source_reference_rejects_credential_material(source_ref: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        _material(source_ref=source_ref)


def test_source_reference_fails_closed_when_percent_decoding_exceeds_bound() -> None:
    source_ref = "https://example.test/book.pdf?token=secret"
    for _ in range(7):
        source_ref = quote(source_ref, safe="")

    with pytest.raises(ValueError, match="encoding depth"):
        _material(source_ref=source_ref)


def test_durable_payload_semantic_tamper_fails_closed_on_read(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(),
    )
    with tasks.store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["title"] = "Підмінений, але структурно валідний заголовок"
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), task.task_id),
        )

    with pytest.raises(ValueError, match="invalid durable study task payload"):
        queue.get(task.task_id)


def test_payload_is_reference_only_and_does_not_capture_document_or_prompt_text(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(),
    )

    payload = tasks.get(task.task_id).payload

    assert payload["evidence_policy"] == "source_bound_v1"
    assert len(payload["study_fingerprint"]) == 64
    assert "content" not in payload
    assert "document_text" not in payload
    assert "prompt" not in payload
    assert "response" not in payload
