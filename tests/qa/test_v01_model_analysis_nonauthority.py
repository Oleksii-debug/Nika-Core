from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_source_settings import V01SourceSettings


def _local() -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 30,
        "revision": 0,
    }


def test_worker_model_wording_cannot_change_deterministic_checker_verdict(
    tmp_path: Path,
) -> None:
    """Advisory model text must not enter the canonical checker comparison domain."""

    config = AppConfig(database_path=tmp_path / "Профіль" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()

    source_root = tmp_path / "Джерела"
    source_root.mkdir()
    (source_root / "a.txt").write_text("same bounded source evidence", encoding="utf-8")
    (source_root / "b.txt").write_text("same bounded source evidence", encoding="utf-8")

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
    assert models.configure(_local()).status == "completed"

    payload = sources.prepare_task_payload({"command": "Compare the two declared sources."})
    payload = models.prepare_task_payload(payload)
    task_id = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        payload = json.loads(request.content.decode("utf-8"))
        user_text = str(payload["messages"][-1]["content"])
        if "source_worker_model_analysis" in user_text:
            calls += 1
            answer = "advisory wording A" if calls == 1 else "different advisory wording B"
        elif "checker_model_synthesis" in user_text:
            answer = "checker advisory synthesis"
        else:
            raise AssertionError("unexpected model stage")
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "message": {"role": "assistant", "content": answer},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    definitions = AgentDefinitionRepository(store)
    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        source_settings=sources,
        model_runtime_factory=V01BoundModelRuntimeFactory(
            store=store,
            definitions=definitions,
            client_factory=client_factory,
        ),
    )
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": "Compare the two declared sources."},
            )
        )
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert calls == 2

    team_result = result.output["team_result"]
    checker = team_result["checker"]["output"]["checker_summary"]
    assert checker["status"] == "agree"
    assert checker["agreements"] == ["result_set"]
    assert checker["differences"] == []
    assert team_result["checker_output_validated"] is True

    # Model wording may remain available as explicitly advisory output, but it must
    # not have changed the deterministic source result/checker authority.
    worker_why = [
        worker["output"]["result_set"]["items"][0]["why_matched"]
        for worker in team_result["workers"]
    ]
    assert worker_why == [
        "bounded declared local source",
        "bounded declared local source",
    ]
