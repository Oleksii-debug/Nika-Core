from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_source_settings import V01SourceSettings


def test_local_model_result_is_durable_and_restart_readable_without_reinference(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "profile" / "nika.db")
    store = SQLiteStore(config.database_path)
    store.initialize()

    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "a.txt").write_text("same local evidence", encoding="utf-8")
    (source_root / "b.txt").write_text("same local evidence", encoding="utf-8")
    sources = V01SourceSettings(store, config)
    assert sources.configure(
        {
            "root": str(source_root),
            "source_a": "a.txt",
            "source_b": "b.txt",
            "revision": 0,
        }
    ).status == "completed"

    models = V01ModelSettings(store)
    assert models.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "dev97-local-model",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    ).status == "completed"

    command = "Compare the two declared sources."
    payload = models.prepare_task_payload(
        sources.prepare_task_payload({"command": command})
    )
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    )
    queue.transition(task.task_id, TaskState.READY)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "localhost"
        assert request.url.path == "/api/chat"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "dev97-local-model"
        assert body["stream"] is False
        calls.append(str(body["model"]))
        user_text = str(body["messages"][-1]["content"])
        answer = (
            "durable checker synthesis"
            if "checker_model_synthesis" in user_text
            else "bounded worker analysis"
        )
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": answer},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        client_factory=client_factory,
    )
    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        source_settings=sources,
        model_runtime_factory=factory,
    )
    result = asyncio.run(
        TaskRuntimeCoordinator(queue, AuditLog(store)).start(
            runtime,
            RuntimeRequest(
                task_id=task.task_id,
                thread_id=f"desktop-{task.task_id}",
                payload={"command": command},
            ),
        )
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert calls == ["dev97-local-model"] * 3
    team_id = str(result.output["team_id"])
    checker_member_id = str(result.output["team_result"]["checker"]["member_id"])
    expected_analysis = {
        "text": "durable checker synthesis",
        "provider_id": "ollama",
        "provider_kind": "local",
        "model": "dev97-local-model",
    }
    assert result.output["team_result"]["checker"]["output"]["model_analysis"] == (
        expected_analysis
    )

    durable_checker = MultiAgentStore(store).member_result(team_id, checker_member_id)
    assert durable_checker.outcome == "completed"
    assert durable_checker.payload["model_analysis"] == expected_analysis
    task_events = AuditLog(store).list_for(entity_type="task", entity_id=task.task_id)
    assert "v01.model.bound" in [event.event_type for event in task_events]
    assert task_events[-1].event_type == "runtime.finished"
    assert task_events[-1].payload["outcome"] == RuntimeOutcome.COMPLETED.value

    def forbidden_client_factory(**kwargs: object) -> httpx.AsyncClient:
        raise AssertionError(f"completed restart must not re-infer: {kwargs!r}")

    restarted_store = SQLiteStore(store.path)
    restarted_sources = V01SourceSettings(restarted_store, config)
    restarted_factory = V01BoundModelRuntimeFactory(
        store=restarted_store,
        definitions=AgentDefinitionRepository(restarted_store),
        client_factory=forbidden_client_factory,
    )
    restarted_runtime = V01PackagedThreeAgentRuntime(
        store=restarted_store,
        config=config,
        source_settings=restarted_sources,
        model_runtime_factory=restarted_factory,
    )
    reconstructed = asyncio.run(
        restarted_runtime.run(
            RuntimeRequest(
                task_id=task.task_id,
                thread_id=f"desktop-{task.task_id}",
                payload={"command": command},
            )
        )
    )

    assert reconstructed.outcome is RuntimeOutcome.COMPLETED
    assert reconstructed.output == result.output
    assert (
        MultiAgentStore(restarted_store).member_result(
            team_id,
            checker_member_id,
        ).payload["model_analysis"]
        == expected_analysis
    )
    assert calls == ["dev97-local-model"] * 3
