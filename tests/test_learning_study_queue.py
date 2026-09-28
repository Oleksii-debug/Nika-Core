from __future__ import annotations

import json
from contextlib import contextmanager
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


class _BehavioralDigest(str):
    def __len__(self) -> int:
        raise AssertionError("digest length executed before exact-type validation")

    def lower(self) -> str:
        raise AssertionError("digest lower executed before exact-type validation")


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


def test_recovery_refuses_task_that_left_created_after_scan(
    tmp_path,
    monkeypatch,
) -> None:
    _, tasks, queue = _services(tmp_path)
    interrupted = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="raced-recovery", title="Recovery race"),
    )
    with tasks.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            (TaskState.CREATED.value, interrupted.task_id),
        )

    original_match = queue._matching_task_ids

    def raced_match(*, workspace_id, agent_id, state, limit):
        selected = original_match(
            workspace_id=workspace_id,
            agent_id=agent_id,
            state=state,
            limit=limit,
        )
        assert selected == (interrupted.task_id,)
        tasks.transition(interrupted.task_id, TaskState.READY)
        tasks.transition(interrupted.task_id, TaskState.RUNNING)
        tasks.transition(interrupted.task_id, TaskState.FAILED)
        return selected

    monkeypatch.setattr(queue, "_matching_task_ids", raced_match)

    with pytest.raises(ValueError, match="state changed before transition"):
        queue.recover_created(limit=1)

    assert tasks.get(interrupted.task_id).state is TaskState.FAILED
    with tasks.store.connection() as conn:
        revived = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id = ? AND previous_state = ? AND new_state = ?",
            (
                interrupted.task_id,
                TaskState.FAILED.value,
                TaskState.READY.value,
            ),
        ).fetchone()["count"]
    assert revived == 0


def test_resume_refuses_task_that_changed_after_observation(
    tmp_path,
    monkeypatch,
) -> None:
    _, tasks, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="raced-resume", title="Resume race"),
    )
    queue.start(task.task_id)
    tasks.transition(task.task_id, TaskState.BLOCKED)

    real_get = queue.get
    first_read = True

    def raced_get(task_id):
        nonlocal first_read
        observed = real_get(task_id)
        if first_read:
            first_read = False
            assert observed.state is TaskState.BLOCKED
            tasks.transition(task_id, TaskState.FAILED)
        return observed

    monkeypatch.setattr(queue, "get", raced_get)

    with pytest.raises(ValueError, match="state changed before transition"):
        queue.resume(task.task_id)

    assert tasks.get(task.task_id).state is TaskState.FAILED
    with tasks.store.connection() as conn:
        revived = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id = ? AND previous_state = ? AND new_state = ?",
            (task.task_id, TaskState.FAILED.value, TaskState.READY.value),
        ).fetchone()["count"]
    assert revived == 0


def test_listing_and_recovery_are_not_starved_by_500_newer_ordinary_tasks(
    tmp_path,
) -> None:
    path, tasks, queue = _services(tmp_path)
    older_ready = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="older-ready", title="Старіший матеріал"),
    )
    interrupted = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="recover-old", title="Перерване навчання"),
    )
    with tasks.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            (TaskState.CREATED.value, interrupted.task_id),
        )
    for _ in range(501):
        tasks.create(
            workspace_id="noise",
            agent_id="ordinary-worker",
            payload={"kind": "ordinary"},
        )

    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    fresh = StudyQueue(TaskQueue(fresh_store))

    selected = fresh.list_recent(workspace_id="study", agent_id="reader", limit=2)
    recovered = fresh.recover_created()

    assert [item.task_id for item in selected] == [interrupted.task_id, older_ready.task_id]
    assert [item.task_id for item in recovered] == [interrupted.task_id]
    assert fresh.get(interrupted.task_id).state is TaskState.READY


def test_sparse_recovery_uses_fixed_size_keyset_pages(tmp_path) -> None:
    path, tasks, queue = _services(tmp_path)
    interrupted = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="paged-recovery", title="Старий матеріал"),
    )
    with tasks.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            (TaskState.CREATED.value, interrupted.task_id),
        )
    for _ in range(300):
        tasks.create(
            workspace_id="noise",
            agent_id="ordinary-worker",
            payload={"kind": "ordinary"},
        )

    class TracingSQLiteStore(SQLiteStore):
        def __init__(self, db_path) -> None:
            super().__init__(db_path)
            self.statements: list[str] = []

        @contextmanager
        def connection(self):
            with super().connection() as conn:
                conn.set_trace_callback(self.statements.append)
                yield conn

    fresh_store = TracingSQLiteStore(path)
    fresh_store.initialize()
    fresh_store.statements.clear()
    fresh = StudyQueue(TaskQueue(fresh_store))

    recovered = fresh.recover_created(limit=1)
    scan_statements = [
        statement
        for statement in fresh_store.statements
        if statement.startswith(
            "SELECT task_id, payload_json, updated_at, created_at FROM tasks"
        )
    ]

    assert [item.task_id for item in recovered] == [interrupted.task_id]
    assert len(scan_statements) >= 3
    assert all(" LIMIT 128" in statement for statement in scan_statements)


def test_tampered_created_study_task_is_not_transitioned_during_recovery(tmp_path) -> None:
    path, tasks, queue = _services(tmp_path)
    interrupted = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="tampered-created", title="До підміни"),
    )
    with tasks.store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (interrupted.task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["title"] = "Підмінений durable payload"
        conn.execute(
            "UPDATE tasks SET state = ?, payload_json = ? WHERE task_id = ?",
            (
                TaskState.CREATED.value,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                interrupted.task_id,
            ),
        )
        before = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id = ? AND previous_state = ? AND new_state = ?",
            (
                interrupted.task_id,
                TaskState.CREATED.value,
                TaskState.READY.value,
            ),
        ).fetchone()["count"]

    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    fresh_tasks = TaskQueue(fresh_store)
    fresh = StudyQueue(fresh_tasks)

    with pytest.raises(ValueError, match="invalid durable study task payload"):
        fresh.recover_created()

    assert fresh_tasks.get(interrupted.task_id).state is TaskState.CREATED
    with fresh_store.connection() as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id = ? AND previous_state = ? AND new_state = ?",
            (
                interrupted.task_id,
                TaskState.CREATED.value,
                TaskState.READY.value,
            ),
        ).fetchone()["count"]
    assert after == before


def test_extra_durable_content_fails_before_recovery_mutation(tmp_path) -> None:
    path, tasks, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="extra-content"),
    )
    with tasks.store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["prompt"] = "sensitive prompt must never be durable study evidence"
        conn.execute(
            "UPDATE tasks SET state = ?, payload_json = ? WHERE task_id = ?",
            (
                TaskState.CREATED.value,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                task.task_id,
            ),
        )

    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    fresh_tasks = TaskQueue(fresh_store)
    fresh = StudyQueue(fresh_tasks)

    with pytest.raises(ValueError, match="invalid durable study task payload"):
        fresh.recover_created(limit=1)

    assert fresh_tasks.get(task.task_id).state is TaskState.CREATED
    with fresh_store.connection() as conn:
        ready_events = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events "
            "WHERE task_id = ? AND previous_state = ? AND new_state = ?",
            (task.task_id, TaskState.CREATED.value, TaskState.READY.value),
        ).fetchone()["count"]
    assert ready_events == 1  # only the original enqueue transition, never recovery


def test_explicit_null_optional_field_is_not_canonical_durable_evidence(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    task = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="null-shape", source_version=None),
    )
    with tasks.store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["source_version"] = None
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), task.task_id),
        )

    with pytest.raises(ValueError, match="invalid durable study task payload"):
        queue.get(task.task_id)


def test_material_requires_an_immutable_source_identity() -> None:
    with pytest.raises(ValueError, match="immutable study identity"):
        _material(source_version=None, content_sha256=None)

    assert _material(content_sha256=None).source_version == "edition-1"
    assert _material(source_version=None).content_sha256 == "a" * 64


def test_material_requires_exact_sha256_carrier_before_digest_operations() -> None:
    digest = _BehavioralDigest("a" * 64)

    with pytest.raises(TypeError, match="content_sha256 must be text"):
        _material(content_sha256=digest)


def test_enqueue_revalidates_mutated_sha256_before_durable_write(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    material = _material(material_id="mutated-digest")
    object.__setattr__(material, "content_sha256", _BehavioralDigest("a" * 64))

    with pytest.raises(TypeError, match="content_sha256 must be text"):
        queue.enqueue(
            workspace_id="study",
            agent_id="reader",
            material=material,
        )

    with tasks.store.connection() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS count FROM tasks"
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events"
        ).fetchone()["count"]
    assert task_count == 0
    assert event_count == 0


@pytest.mark.parametrize(
    "source_ref",
    [
        "https://example.test/book.pdf?api_key=secret",
        "https://user:secret@example.test/book.pdf",
        "https://example.test/book.pdf?%74oken=secret",
        "https://example.test/book.pdf?api-key=secret",
        "https://example.test/book.pdf?access-token=secret",
        "https://example.test/book.pdf?X-Amz-Credential=secret",
        "https://example.test/book.pdf?X-Amz-Signature=secret",
        "https://example.test/book.pdf?X-Amz-Security-Token=secret",
        "https://example.test/book.pdf?X-Goog-Credential=secret",
        "https://example.test/book.pdf?X-Goog-Signature=secret",
        "https://example.test/book.pdf?X%2DAmz%2DSignature=secret",
        "https://example.test/book.pdf#access-token=secret",
        "https://example.test/book.pdf#X-Amz-Signature=secret",
        "https://example.test/book.pdf#X-Goog-Credential=secret",
        "s3://bucket/book.pdf?X-Amz-Signature=secret",
        "gs://bucket/book.pdf?X-Goog-Credential=secret",
        "ftp://user:secret@example.test/book.pdf",
        "Authorization: Bearer secret",
        "https%3A%2F%2Fexample.test%2Fbook.pdf%3Ftoken%3Dsecret",
    ],
)
def test_source_reference_rejects_credential_material(source_ref: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        _material(source_ref=source_ref)


def test_public_query_reference_remains_stable_across_restart(tmp_path) -> None:
    path, tasks, queue = _services(tmp_path)
    public_ref = (
        "https://example.test/book.pdf?chapter=4"
        "&X-Amz-Date=20260929T000000Z&X-Goog-Algorithm=GOOG4-RSA-SHA256"
    )

    created = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(source_ref=public_ref),
    )
    raw = tasks.get(created.task_id).payload
    assert raw["source_ref"] == public_ref

    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    restored = StudyQueue(TaskQueue(fresh_store)).get(created.task_id)
    assert restored.material.source_ref == public_ref


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

def test_enqueue_revalidates_mutated_material_before_durable_write(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    material = _material(material_id="mutated-secret-source")
    object.__setattr__(
        material,
        "source_ref",
        "s3://bucket/book.pdf?X-Amz-Signature=secret",
    )

    with pytest.raises(ValueError, match="credential"):
        queue.enqueue(
            workspace_id="study",
            agent_id="reader",
            material=material,
        )

    with tasks.store.connection() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS count FROM tasks"
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events"
        ).fetchone()["count"]
    assert task_count == 0
    assert event_count == 0


def test_enqueue_requires_exact_material_kind_before_durable_write(tmp_path) -> None:
    _, tasks, queue = _services(tmp_path)
    material = _material(material_id="mutated-kind")
    object.__setattr__(material, "kind", "book")

    with pytest.raises(TypeError, match="kind must be exact StudyMaterialKind"):
        queue.enqueue(
            workspace_id="study",
            agent_id="reader",
            material=material,
        )

    with tasks.store.connection() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS count FROM tasks"
        ).fetchone()["count"]
        event_count = conn.execute(
            "SELECT COUNT(*) AS count FROM task_events"
        ).fetchone()["count"]
    assert task_count == 0
    assert event_count == 0


def test_oidc_id_token_references_are_rejected_but_count_remains_public(tmp_path) -> None:
    for source_ref in (
        "https://example.test/book.pdf?id_token=eyJ_SECRET&state=stable",
        "https://example.test/book.pdf#id-token=eyJ_SECRET&state=stable",
        "idtoken=eyJ_SECRET",
        "https%3A%2F%2Fexample.test%2Fbook.pdf%3Fid_token%3DeyJ_SECRET",
    ):
        with pytest.raises(ValueError, match="credential"):
            _material(source_ref=source_ref)

    safe_ref = "https://example.test/book.pdf?id_token_count=3"
    path, _, queue = _services(tmp_path)
    created = queue.enqueue(
        workspace_id="study",
        agent_id="reader",
        material=_material(material_id="oidc-count", source_ref=safe_ref),
    )
    fresh_store = SQLiteStore(path)
    fresh_store.initialize()
    fresh = StudyQueue(TaskQueue(fresh_store)).get(created.task_id)

    assert fresh.material.source_ref == safe_ref

