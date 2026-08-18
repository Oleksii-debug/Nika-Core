from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition, require_transition
from nika_core.model_gateway.base import ModelRequest
from nika_core.model_gateway.mock import MockProvider


def test_created_can_become_ready() -> None:
    assert can_transition(TaskState.CREATED, TaskState.READY)


def test_completed_cannot_run_again() -> None:
    assert not can_transition(TaskState.COMPLETED, TaskState.RUNNING)
    with pytest.raises(ValueError):
        require_transition(TaskState.COMPLETED, TaskState.RUNNING)


def test_registry_requires_increasing_version() -> None:
    registry = AgentRegistry()
    registry.register(AgentDefinition("a1", "Agent", 1, "Test"))
    with pytest.raises(ValueError):
        registry.register(AgentDefinition("a1", "Agent", 1, "Again"))
    registry.register(AgentDefinition("a1", "Agent", 2, "Updated"))
    assert registry.get("a1").version == 2


def test_queue_checkpoint_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    assert store.schema_version() == 6
    queue = TaskQueue(store)
    task = queue.create(workspace_id="lab", agent_id="a1", payload={"x": 1})
    queue.transition(task.task_id, TaskState.READY)
    assert queue.count_ready == 1
    service = CheckpointService(store)
    saved = service.save(task_id=task.task_id, stage="plan", payload={"step": 2})
    assert service.latest(task.task_id) == saved


def test_invalid_transition_is_atomic(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="lab", agent_id="a1")
    with pytest.raises(ValueError):
        queue.transition(task.task_id, TaskState.COMPLETED)


def test_mock_provider_is_deterministic() -> None:
    provider = MockProvider()
    response = provider.chat(ModelRequest(messages=({"role": "user", "content": "hello"},)))
    assert provider.health()
    assert response.text == "MOCK:hello"
    assert response.provider == "mock"
