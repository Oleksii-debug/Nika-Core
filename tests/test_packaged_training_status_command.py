from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    packaged_training_status_target,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.training_runtime import TrainingStatusService
from nika_core.ui.bridge_models import UIResult
from scripts.nika_windows import _training_status_result

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "11111111-1111-4111-8111-111111111111"


class _OrdinaryHandler:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary-task",
            focus_id="tasks-heading",
        )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "packaged training status.db")
    store.initialize()
    return store


def _created_task_id(store: SQLiteStore) -> str:
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "bounded training"},
    ).task_id


def _checkpoint_payload(
    *,
    reason: str | None = "resource_revalidation:memory_limit",
) -> dict[str, object]:
    return {
        "schema_version": 4,
        "job_id": "job-1",
        "job_fingerprint": "a" * 64,
        "frozen_package_sha256": "b" * 64,
        "training_material_sha256": "c" * 64,
        "scale_authorization_sha256": "d" * 64,
        "next_step": 1,
        "resume_state": {"last_step": 0},
        "candidate_artifact_ref": "candidate-1",
        "candidate_sha256": None,
        "reason": reason,
    }


def _task_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
    assert row is not None
    return int(row["count"])


@pytest.mark.parametrize(
    "command",
    (
        f"training status {TASK_ID}",
        f"Show training status {TASK_ID}.",
        f"статус навчання {TASK_ID}",
        f"Покажи статус навчання {TASK_ID}!",
    ),
)
def test_training_status_recognizer_accepts_only_explicit_task_commands(
    command: str,
) -> None:
    assert packaged_training_status_target(command) == TASK_ID


@pytest.mark.parametrize(
    "command",
    (
        "explain training status",
        "поясни статус навчання системи",
        "show training report",
        "навчання моделі",
    ),
)
def test_training_status_recognizer_does_not_capture_generic_task_text(
    command: str,
) -> None:
    assert packaged_training_status_target(command) is None


@pytest.mark.parametrize(
    "command",
    (
        "training status",
        "статус навчання",
        "training status not-a-uuid",
        "покажи статус навчання 11111111111111111111111111111111",
    ),
)
def test_training_status_recognizer_rejects_missing_or_noncanonical_task_id(
    command: str,
) -> None:
    with pytest.raises(PackagedProductJourneyError, match="UUID"):
        packaged_training_status_target(command)


def test_packaged_training_status_is_read_only_and_bypasses_ordinary_task(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _created_task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=_checkpoint_payload(),
    )
    service = TrainingStatusService(checkpoints)
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
        training_status_handler=lambda target: _training_status_result(service, target),
    )
    before = _task_count(store)

    result = router.create({"command": f"Покажи статус навчання {task_id}"})

    assert result.status == "completed"
    assert result.focus_id == "logs-heading"
    assert "Стан навчання Nika." in result.message
    assert f"Завдання: {task_id}" in result.message
    assert "Стан: призупинено (paused)" in result.message
    assert "resource_revalidation:memory_limit" in result.message
    assert "Обмеження доказовості:" in result.message
    assert "candidate-1" not in result.message
    assert ordinary.calls == []
    assert router.active_project_id is None
    assert _task_count(store) == before


def test_training_status_preserves_current_product_project_selection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _created_task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=_checkpoint_payload(),
    )
    repository = ProductProjectRepository(store)
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        training_status_handler=lambda target: _training_status_result(
            TrainingStatusService(checkpoints),
            target,
        ),
    )
    product_command = "Створи застосунок для доступного контролю моделей"
    project_id = product_project_identity(product_command)
    assert router.create({"command": product_command}).status == "completed"
    before = repository.get(project_id)

    result = router.create({"command": f"training status {task_id}"})

    assert result.status == "completed"
    assert router.active_project_id == project_id
    assert repository.get(project_id) == before
    assert ordinary.calls == []


def test_explicit_training_status_fails_closed_when_handler_is_not_bound(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
    )

    with pytest.raises(PackagedProductJourneyError, match="Статус навчання недоступний"):
        router.create({"command": f"training status {TASK_ID}"})

    assert ordinary.calls == []


def test_packaged_training_status_reports_absent_checkpoint_without_creating_work(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _created_task_id(store)
    service = TrainingStatusService(CheckpointService(store))
    before = _task_count(store)

    result = _training_status_result(service, task_id)

    assert result.status == "completed"
    assert result.focus_id == "logs-heading"
    assert result.message == (
        "Для цього task_id немає збереженого durable checkpoint стану навчання."
    )
    assert _task_count(store) == before


def test_packaged_training_status_bounds_corrupt_checkpoint_detail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _created_task_id(store)
    checkpoints = CheckpointService(store)
    checkpoints.save(
        task_id=task_id,
        stage="training_runtime/v4/paused",
        payload=_checkpoint_payload(reason="C:\\private\\secret-token"),
    )

    result = _training_status_result(TrainingStatusService(checkpoints), task_id)

    assert result.status == "failed"
    assert result.focus_id == "logs-heading"
    assert result.message == "Не вдалося безпечно прочитати стан навчання."
    assert "private" not in result.message.casefold()
    assert "secret" not in result.message.casefold()
    assert "token" not in result.message.casefold()


def test_windows_composition_wires_canonical_training_status_service() -> None:
    source = (ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")

    assert "TrainingStatusService(CheckpointService(store))" in source
    assert "training_status_handler=lambda task_id: _training_status_result(" in source
