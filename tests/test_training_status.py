from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.training_runtime import (
    TrainingRunState,
    TrainingStatusError,
    TrainingStatusService,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "training status.db")
    store.initialize()
    return store


def _task_id(store: SQLiteStore) -> str:
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "bounded training"},
    ).task_id


def _payload(
    *,
    next_step: int = 2,
    reason: str | None = "resource_revalidation:cpu_limit",
    candidate_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 4,
        "job_id": "job-1",
        "job_fingerprint": "a" * 64,
        "frozen_package_sha256": "b" * 64,
        "training_material_sha256": "c" * 64,
        "scale_authorization_sha256": "e" * 64,
        "next_step": next_step,
        "resume_state": {"last_step": max(0, next_step - 1)},
        "candidate_artifact_ref": "candidate-1",
        "candidate_sha256": candidate_sha256,
        "reason": reason,
    }


def test_training_status_projects_only_bounded_checkpoint_truth(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    checkpoint = checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=_payload(),
    )

    status = TrainingStatusService(checkpoints).read(task_id)

    assert status is not None
    assert status.task_id == task_id
    assert status.state is TrainingRunState.PAUSED
    assert status.next_step == 2
    assert status.reason == "resource_revalidation:cpu_limit"
    assert status.checkpoint_id == checkpoint.checkpoint_id
    rendered = status.render_text()
    assert f"Завдання: {task_id}" in rendered
    assert "Стан: призупинено (paused)" in rendered
    assert "Наступний крок: 2" in rendered
    assert "Обмеження доказовості:" in rendered
    assert "resume_state" not in rendered
    assert "candidate-1" not in rendered
    assert "a" * 64 not in rendered
    assert "b" * 64 not in rendered
    assert "c" * 64 not in rendered
    assert "e" * 64 not in rendered


def test_training_status_returns_none_without_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)

    assert TrainingStatusService(CheckpointService(store)).read(task_id) is None


def test_training_status_never_falls_back_past_newer_nontraining_checkpoint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=_payload(),
    )
    checkpoints.save(
        task_id=task_id,
        stage="runtime/v1/paused",
        payload={"safe": True},
    )

    with pytest.raises(TrainingStatusError, match="not Loop-C training state"):
        TrainingStatusService(checkpoints).read(task_id)


def test_training_status_rejects_superseded_v3_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    legacy = _payload()
    legacy["schema_version"] = 3
    legacy.pop("scale_authorization_sha256")
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v3/paused",
        payload=legacy,
    )

    with pytest.raises(TrainingStatusError, match="not Loop-C training state"):
        TrainingStatusService(checkpoints).read(task_id)


@pytest.mark.parametrize(
    "task_id",
    (
        "",
        "not-a-uuid",
        "00000000000000000000000000000000",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ),
)
def test_training_status_requires_canonical_task_uuid(
    tmp_path: Path,
    task_id: str,
) -> None:
    service = TrainingStatusService(CheckpointService(_store(tmp_path)))

    with pytest.raises(TrainingStatusError, match="canonical UUID"):
        service.read(task_id)


@pytest.mark.parametrize(
    ("stage", "payload"),
    (
        ("training_runtime/v4/waiting", _payload()),
        ("training_runtime/v4/unknown", _payload()),
        (
            "training_runtime/v4/paused",
            {**_payload(), "schema_version": 2},
        ),
        (
            "training_runtime/v4/paused",
            {**_payload(), "scale_authorization_sha256": "not-a-digest"},
        ),
        (
            "training_runtime/v4/paused",
            {**_payload(), "next_step": True},
        ),
        (
            "training_runtime/v4/paused",
            {**_payload(), "reason": "C:\\private\\secret-token"},
        ),
        (
            "training_runtime/v4/paused",
            {**_payload(), "candidate_sha256": "d" * 64},
        ),
        (
            "training_runtime/v4/completed",
            _payload(next_step=1, reason=None, candidate_sha256=None),
        ),
    ),
)
def test_training_status_fails_closed_on_malformed_latest_training_checkpoint(
    tmp_path: Path,
    stage: str,
    payload: dict[str, object],
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(task_id=task_id, stage=stage, payload=payload)

    with pytest.raises(TrainingStatusError):
        TrainingStatusService(checkpoints).read(task_id)


@pytest.mark.parametrize("field_name", ("job_id", "candidate_artifact_ref"))
def test_training_status_rejects_noncanonical_training_identifiers(
    tmp_path: Path,
    field_name: str,
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    payload = _payload()
    payload[field_name] = " noncanonical "
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=payload,
    )

    with pytest.raises(TrainingStatusError, match="identity"):
        TrainingStatusService(checkpoints).read(task_id)


def test_training_status_rejects_resume_state_above_training_byte_limit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    payload = _payload()
    payload["resume_state"] = {"opaque": "x" * (70 * 1024)}
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=payload,
    )

    with pytest.raises(TrainingStatusError, match="result evidence"):
        TrainingStatusService(checkpoints).read(task_id)


def test_training_status_rejects_resume_state_above_training_depth_limit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    root: dict[str, object] = {}
    cursor = root
    for _ in range(13):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    payload = _payload()
    payload["resume_state"] = root
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=payload,
    )

    with pytest.raises(TrainingStatusError, match="result evidence"):
        TrainingStatusService(checkpoints).read(task_id)


def test_training_status_accepts_completed_checkpoint_without_exposing_candidate_digest(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/completed",
        payload=_payload(
            next_step=3,
            reason=None,
            candidate_sha256="d" * 64,
        ),
    )

    status = TrainingStatusService(checkpoints).read(task_id)

    assert status is not None
    assert status.state is TrainingRunState.COMPLETED
    assert status.next_step == 3
    assert "d" * 64 not in status.render_text()
