from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.modes import IntelligenceModePolicy
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_source_settings import V01SourceSettings


class _StaticResolver:
    def __init__(self, material: str) -> None:
        self.material = material
        self.references: list[str] = []

    def resolve(self, credential_ref: str) -> str:
        self.references.append(credential_ref)
        return self.material


def _environment(
    tmp_path: Path,
) -> tuple[SQLiteStore, AppConfig, V01SourceSettings, V01ModelSettings]:
    config = AppConfig(database_path=tmp_path / "Профіль" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    source_root = tmp_path / "Джерела"
    source_root.mkdir()
    (source_root / "a.txt").write_text("same bounded source evidence", encoding="utf-8")
    (source_root / "b.txt").write_text("same bounded source evidence", encoding="utf-8")
    sources = V01SourceSettings(store, config)
    assert (
        sources.configure(
            {
                "root": str(source_root),
                "source_a": "a.txt",
                "source_b": "b.txt",
                "revision": 0,
            }
        ).status
        == "completed"
    )
    return store, config, sources, V01ModelSettings(store)


def _local(
    *,
    revision: int = 0,
    model: str = "qwen3:8b",
    timeout_seconds: float = 30,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": model,
        "base_url": "http://localhost:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": timeout_seconds,
        "revision": revision,
    }


def _api(
    *,
    revision: int = 0,
    private_data_allowed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": "api-model",
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_PACKAGED_TEST_REFERENCE",
        "private_data_allowed": private_data_allowed,
        "timeout_seconds": 30,
        "revision": revision,
    }


def _create_bound_task(
    *,
    store: SQLiteStore,
    sources: V01SourceSettings,
    models: V01ModelSettings,
    command: str = "Compare the two declared sources.",
) -> str:
    payload = sources.prepare_task_payload({"command": command})
    payload = models.prepare_task_payload(payload)
    return TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id


def _runtime(
    *,
    store: SQLiteStore,
    config: AppConfig,
    sources: V01SourceSettings,
    client_factory,
    resolver: _StaticResolver | None = None,
) -> V01PackagedThreeAgentRuntime:
    definitions = AgentDefinitionRepository(store)
    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=definitions,
        credential_resolver=resolver,
        client_factory=client_factory,
        intelligence_policy=IntelligenceModePolicy(
            external_api_enabled=True,
            external_provider_id="configured-api",
        ),
    )
    return V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        source_settings=sources,
        model_runtime_factory=factory,
    )


def _run(runtime: V01PackagedThreeAgentRuntime, task_id: str) -> object:
    return asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task_id,
                thread_id=f"desktop-{task_id}",
                payload={"command": "Compare the two declared sources."},
            )
        )
    )


def test_packaged_selected_local_model_drives_two_workers_and_checker(
    tmp_path: Path,
) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_local()).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content.decode("utf-8"))
        user_text = str(payload["messages"][-1]["content"])
        stage = (
            "checker"
            if "checker_model_synthesis" in user_text
            else "worker"
            if "source_worker_model_analysis" in user_text
            else "unknown"
        )
        calls.append((request.url.path, str(payload["model"]), stage))
        if stage == "checker":
            answer = "checker synthesis"
        elif '"source_id":"source-a"' in user_text:
            answer = "worker A variable relevance text"
        elif '"source_id":"source-b"' in user_text:
            answer = "worker B materially different relevance text"
        else:
            answer = "unexpected worker analysis"
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

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
        ),
        task_id,
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert calls == [
        ("/api/chat", "qwen3:8b", "worker"),
        ("/api/chat", "qwen3:8b", "worker"),
        ("/api/chat", "qwen3:8b", "checker"),
    ]
    team_result = result.output["team_result"]
    assert team_result["checker_output_validated"] is True
    for worker in team_result["workers"]:
        item = worker["output"]["result_set"]["items"][0]
        assert item["snippet"] == "same bounded source evidence"
        assert item["why_matched"] == "bounded declared local source"
    model_analysis = team_result["checker"]["output"]["model_analysis"]
    assert model_analysis == {
        "text": "checker synthesis",
        "provider_id": "ollama",
        "provider_kind": "local",
        "model": "qwen3:8b",
    }


def test_packaged_restart_uses_task_frozen_model_after_default_changes(
    tmp_path: Path,
) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_local(model="frozen-model")).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    assert models.configure(_local(revision=1, model="new-default")).status == "completed"
    models_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        models_seen.append(str(payload["model"]))
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "message": {"role": "assistant", "content": "stable analysis"},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    restarted_store = SQLiteStore(store.path)
    restarted_sources = V01SourceSettings(restarted_store, config)
    restarted = _runtime(
        store=restarted_store,
        config=config,
        sources=restarted_sources,
        client_factory=client_factory,
    )
    result = _run(restarted, task_id)

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert models_seen == ["frozen-model"] * 3
    with restarted_store.connection() as conn:
        bound = conn.execute(
            "SELECT selection_json FROM v01_task_model_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert bound is not None
    assert json.loads(bound["selection_json"])["model"] == "frozen-model"


def test_packaged_api_failure_never_switches_to_local_provider(tmp_path: Path) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_api()).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503, json={"error": "fixture unavailable"})

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
            resolver=_StaticResolver("fixture-material"),
        ),
        task_id,
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert paths
    assert set(paths) == {"/v1/chat/completions"}
    assert "/api/chat" not in paths


def test_packaged_private_api_route_fails_before_credential_or_transport(
    tmp_path: Path,
) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_api(private_data_allowed=False)).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    paths: list[str] = []
    resolver = _StaticResolver("fixture-material")

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        raise AssertionError("private route must fail before transport")

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
            resolver=resolver,
        ),
        task_id,
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert paths == []
    assert resolver.references == []


def test_packaged_api_credential_material_never_enters_durable_task_team_or_audit(
    tmp_path: Path,
) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_api()).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    secret = "fixture-material"
    resolver = _StaticResolver(secret)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        payload = json.loads(request.content.decode("utf-8"))
        stage = str(payload["messages"][-1]["content"])
        answer = "checker synthesis" if "checker_model_synthesis" in stage else "safe relevance"
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"content": answer}}],
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
            resolver=resolver,
        ),
        task_id,
    )
    assert result.outcome is RuntimeOutcome.COMPLETED

    with store.connection() as conn:
        durable = [
            *(str(row[0]) for row in conn.execute("SELECT payload_json FROM tasks")),
            *(str(row[0]) for row in conn.execute("SELECT payload_json FROM audit_events")),
            *(
                str(row[0])
                for row in conn.execute("SELECT payload_json FROM multi_agent_handoffs")
            ),
            *(
                str(row[0])
                for row in conn.execute("SELECT payload_json FROM multi_agent_results")
            ),
            *(
                str(row[0])
                for row in conn.execute("SELECT selection_json FROM v01_task_model_bindings")
            ),
        ]
    assert secret not in repr(durable)
    assert resolver.references == ["env:NIKA_PACKAGED_TEST_REFERENCE"] * 3


def test_packaged_local_response_model_substitution_fails_closed(tmp_path: Path) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_local(model="selected-local-model")).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "different-local-model",
                "message": {"role": "assistant", "content": "must not be accepted"},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
        ),
        task_id,
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert 1 <= calls <= 3
    with store.connection() as conn:
        root_states = [
            str(row["state"])
            for row in conn.execute(
                "SELECT state FROM multi_agent_members WHERE parent_id IS NULL"
            )
        ]
    assert root_states
    assert "completed" not in root_states


def test_packaged_api_response_model_substitution_fails_closed(tmp_path: Path) -> None:
    store, config, sources, models = _environment(tmp_path)
    assert models.configure(_api()).status == "completed"
    task_id = _create_bound_task(store=store, sources=sources, models=models)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "different-api-model",
                "choices": [{"message": {"content": "must not be accepted"}}],
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    result = _run(
        _runtime(
            store=store,
            config=config,
            sources=sources,
            client_factory=client_factory,
            resolver=_StaticResolver("fixture-material"),
        ),
        task_id,
    )

    assert result.outcome is RuntimeOutcome.FAILED
    assert 1 <= calls <= 3
    with store.connection() as conn:
        root_states = [
            str(row["state"])
            for row in conn.execute(
                "SELECT state FROM multi_agent_members WHERE parent_id IS NULL"
            )
        ]
    assert root_states
    assert "completed" not in root_states
