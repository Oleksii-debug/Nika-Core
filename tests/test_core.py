from pathlib import Path
import pytest
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentDefinition, AgentRegistry
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition, require_transition
from nika_core.model_gateway.base import ModelRequest
from nika_core.model_gateway.mock import MockProvider

def test_task_state():
    assert can_transition(TaskState.CREATED, TaskState.READY)
    with pytest.raises(ValueError): require_transition(TaskState.COMPLETED, TaskState.RUNNING)

def test_registry_versions():
    r=AgentRegistry(); r.register(AgentDefinition("a1","Agent",1,"Test"))
    with pytest.raises(ValueError): r.register(AgentDefinition("a1","Agent",1,"Again"))
    r.register(AgentDefinition("a1","Agent",2,"Updated")); assert r.get("a1").version==2

def test_queue_checkpoint(tmp_path: Path):
    s=SQLiteStore(tmp_path/"nika.db"); s.initialize(); assert s.schema_version()==1
    q=TaskQueue(s); t=q.create(workspace_id="lab",agent_id="a1",payload={"x":1}); q.transition(t.task_id,TaskState.READY); assert q.count_ready==1
    cp=CheckpointService(s); saved=cp.save(task_id=t.task_id,stage="plan",payload={"step":2}); assert cp.latest(t.task_id)==saved

def test_invalid_transition_atomic(tmp_path: Path):
    s=SQLiteStore(tmp_path/"nika.db"); s.initialize(); q=TaskQueue(s); t=q.create(workspace_id="lab",agent_id="a1")
    with pytest.raises(ValueError): q.transition(t.task_id,TaskState.COMPLETED)

def test_mock_provider():
    p=MockProvider(); r=p.chat(ModelRequest(messages=({"role":"user","content":"hello"},))); assert p.health(); assert r.text=="MOCK:hello"
