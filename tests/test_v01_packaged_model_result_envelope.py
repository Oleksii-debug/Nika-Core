from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider
from nika_core.v01_source_settings import V01SourceSettings

_TASK_ID = "task-1"
_TEAM_ID = "team-1"
_CHECKER_ID = "checker"
_MODEL = "test-model"
_EXPECTED = {"status": "agree", "sources": [{"state": "valid"}, {"state": "valid"}]}
_MODEL_TEXT_CANARY = "MODEL_RESULT_TEXT_CANARY"
_SOURCE_CANARY = "MODEL_RESULT_SOURCE_CANARY"


def _model_backed_payload() -> dict[str, object]:
    return {
        "checker_summary": copy.deepcopy(_EXPECTED),
        "model_analysis": {
            "text": "Bounded model synthesis.",
            "provider_id": "ollama",
            "provider_kind": "local",
            "model": _MODEL,
        },
        "model_analysis_provenance": {
            "schema": "nika.intelligence.provenance.v1",
            "origin": "model",
            "intelligence_mode": "external_local",
            "provider_kind": "local",
            "provider_id": "ollama",
            "model_fingerprint": model_identity_fingerprint(_MODEL),
            "request_correlation_id": f"{_TASK_ID}:v01:{_TEAM_ID}:{_CHECKER_ID}",
            "status": "succeeded",
        },
    }


def _valid(payload: object, *, model_required: bool = False) -> bool:
    return V01PackagedTeamStateProvider._valid_persisted_checker_payload(
        payload,
        expected=_EXPECTED,
        shared_task_id=_TASK_ID,
        team_id=_TEAM_ID,
        root_id=_CHECKER_ID,
        model_required=model_required,
    )


def _base_state(queue: TaskQueue, task_id: str) -> dict[str, object]:
    task = queue.get(task_id)
    return {
        "tasks": [
            {
                "task_id": task.task_id,
                "workspace_id": task.workspace_id,
                "agent_id": task.agent_id,
                "state": task.state.value,
                "command": str(task.payload.get("command", "")),
            }
        ],
        "agents": [],
        "workspaces": [],
        "product_project": None,
    }


def test_checker_envelope_is_bound_to_durable_model_requirement() -> None:
    legacy = {"checker_summary": copy.deepcopy(_EXPECTED)}
    model_backed = _model_backed_payload()

    assert _valid(legacy) is True
    assert _valid(model_backed, model_required=True) is True
    assert _valid(legacy, model_required=True) is False
    assert _valid(model_backed) is False


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_top_level",
        "missing_provenance",
        "analysis_extra",
        "blank_text",
        "oversized_text",
        "provider_mismatch",
        "provider_kind_mismatch",
        "model_mismatch",
        "correlation_mismatch",
        "non_success_provenance",
        "provenance_extra",
    ],
)
def test_checker_envelope_rejects_unbound_or_malformed_model_evidence(mutation: str) -> None:
    payload = _model_backed_payload()
    analysis = payload["model_analysis"]
    provenance = payload["model_analysis_provenance"]
    assert isinstance(analysis, dict)
    assert isinstance(provenance, dict)

    if mutation == "unknown_top_level":
        payload["raw_model_output"] = "must never be accepted"
    elif mutation == "missing_provenance":
        del payload["model_analysis_provenance"]
    elif mutation == "analysis_extra":
        analysis["raw"] = "must never be accepted"
    elif mutation == "blank_text":
        analysis["text"] = "   "
    elif mutation == "oversized_text":
        analysis["text"] = "x" * 2001
    elif mutation == "provider_mismatch":
        analysis["provider_id"] = "different-provider"
    elif mutation == "provider_kind_mismatch":
        analysis["provider_kind"] = "cloud"
    elif mutation == "model_mismatch":
        analysis["model"] = "different-model"
    elif mutation == "correlation_mismatch":
        provenance["request_correlation_id"] = "foreign-task:v01:team-1:checker"
    elif mutation == "non_success_provenance":
        provenance["status"] = "failed"
    elif mutation == "provenance_extra":
        provenance["raw"] = "must never be accepted"
    else:  # pragma: no cover - parametrization is exhaustive.
        raise AssertionError(mutation)

    assert _valid(payload, model_required=True) is False


def test_checker_envelope_rejects_summary_rebinding() -> None:
    payload = _model_backed_payload()
    payload["checker_summary"] = {"status": "disagree"}
    assert _valid(payload, model_required=True) is False


def test_model_backed_packaged_comparison_survives_fresh_store_restart_without_leak(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "profile" / "nika.db")
    store = SQLiteStore(config.database_path)
    store.initialize()

    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "a.txt").write_text(_SOURCE_CANARY, encoding="utf-8")
    (source_root / "b.txt").write_text(_SOURCE_CANARY, encoding="utf-8")
    sources = V01SourceSettings(store, config)
    assert sources.configure(
        {
            "root": str(source_root),
            "source_a": "a.txt",
            "source_b": "b.txt",
            "revision": 0,
        }
    ).status == "completed"

    model_name = "packaged-restart-local-model"
    models = V01ModelSettings(store)
    assert models.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": model_name,
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
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == model_name
        assert body["stream"] is False
        calls.append(str(body["model"]))
        user_text = str(body["messages"][-1]["content"])
        answer = (
            _MODEL_TEXT_CANARY
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

    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        source_settings=sources,
        model_runtime_factory=V01BoundModelRuntimeFactory(
            store=store,
            definitions=AgentDefinitionRepository(store),
            client_factory=client_factory,
        ),
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
    assert calls == [model_name] * 3

    before = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state(queue, task.task_id),
        store=store,
    )()["v01_team_task"]
    assert before is not None
    before_comparison = before["final_result"]["comparison"]
    assert before_comparison["validated"] is True
    assert before_comparison["model_result"] == {
        "text": _MODEL_TEXT_CANARY,
        "provider_id": "ollama",
        "provider_kind": "local",
        "model": model_name,
        "provenance_validated": True,
    }

    restarted_store = SQLiteStore(store.path)
    restarted_queue = TaskQueue(restarted_store)
    restarted_provider = V01PackagedTeamStateProvider(
        base_state=lambda: _base_state(restarted_queue, task.task_id),
        store=restarted_store,
    )
    after = restarted_provider()["v01_team_task"]
    assert after is not None
    assert after["final_result"]["comparison"] == before_comparison
    assert calls == [model_name] * 3

    projected = json.dumps(after, ensure_ascii=False, sort_keys=True)
    assert _MODEL_TEXT_CANARY in projected
    assert model_name in projected
    assert "ollama" in projected
    assert _SOURCE_CANARY not in projected
    assert "model_analysis" not in projected
    assert "model_analysis_provenance" not in projected
    assert "request_correlation_id" not in projected

    team_id = after["team"]["team_id"]
    with restarted_store.connection() as conn:
        row = conn.execute(
            "SELECT result_id, payload_json FROM multi_agent_results "
            "WHERE team_id = ? AND member_id = 'checker'",
            (team_id,),
        ).fetchone()
        assert row is not None
        canonical_persisted = json.loads(row["payload_json"])
        hostile_persisted = copy.deepcopy(canonical_persisted)
        hostile_analysis = hostile_persisted["model_analysis"]
        hostile_provenance = hostile_persisted["model_analysis_provenance"]
        assert isinstance(hostile_analysis, dict)
        assert isinstance(hostile_provenance, dict)
        hostile_provider = "foreign-local-provider"
        hostile_model = "foreign-self-consistent-model"
        hostile_analysis["provider_id"] = hostile_provider
        hostile_analysis["model"] = hostile_model
        hostile_provenance["provider_id"] = hostile_provider
        hostile_provenance["model_fingerprint"] = model_identity_fingerprint(hostile_model)
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (
                json.dumps(hostile_persisted, sort_keys=True, separators=(",", ":")),
                row["result_id"],
            ),
        )

    wrong_frozen_model = restarted_provider()["v01_team_task"]
    assert wrong_frozen_model is not None
    wrong_frozen_comparison = wrong_frozen_model["final_result"]["comparison"]
    assert wrong_frozen_comparison["status"] == "evidence_invalid"
    assert wrong_frozen_comparison["validated"] is False
    assert "model_result" not in wrong_frozen_comparison
    assert _MODEL_TEXT_CANARY not in json.dumps(
        wrong_frozen_model,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert hostile_provider not in json.dumps(
        wrong_frozen_model,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert hostile_model not in json.dumps(
        wrong_frozen_model,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert calls == [model_name] * 3

    persisted = copy.deepcopy(canonical_persisted)
    del persisted["model_analysis"]
    del persisted["model_analysis_provenance"]
    with restarted_store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (
                json.dumps(persisted, sort_keys=True, separators=(",", ":")),
                row["result_id"],
            ),
        )

    downgraded = restarted_provider()["v01_team_task"]
    assert downgraded is not None
    downgraded_comparison = downgraded["final_result"]["comparison"]
    assert downgraded_comparison["status"] == "evidence_invalid"
    assert downgraded_comparison["validated"] is False
    assert "model_result" not in downgraded_comparison
    assert _MODEL_TEXT_CANARY not in json.dumps(downgraded, ensure_ascii=False, sort_keys=True)
    assert calls == [model_name] * 3

    with restarted_store.connection() as conn:
        canonical_provenance = canonical_persisted["model_analysis_provenance"]
        assert isinstance(canonical_provenance, dict)
        canonical_provenance["model_fingerprint"] = "sha256:corrupt"
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (
                json.dumps(canonical_persisted, sort_keys=True, separators=(",", ":")),
                row["result_id"],
            ),
        )

    corrupted = restarted_provider()["v01_team_task"]
    assert corrupted is not None
    corrupted_comparison = corrupted["final_result"]["comparison"]
    assert corrupted_comparison["status"] == "evidence_invalid"
    assert corrupted_comparison["validated"] is False
    assert "model_result" not in corrupted_comparison
    assert _MODEL_TEXT_CANARY not in json.dumps(corrupted, ensure_ascii=False, sort_keys=True)
    assert calls == [model_name] * 3

    with restarted_store.connection() as conn:
        conn.execute(
            "UPDATE multi_agent_results SET payload_json = ? WHERE result_id = ?",
            (
                json.dumps(persisted, sort_keys=True, separators=(",", ":")),
                row["result_id"],
            ),
        )
        task_row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        assert task_row is not None
        task_payload = json.loads(task_row["payload_json"])
        assert "v01_model_selection" in task_payload
        del task_payload["v01_model_selection"]
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (
                json.dumps(task_payload, sort_keys=True, separators=(",", ":")),
                task.task_id,
            ),
        )
        conn.execute(
            "DELETE FROM v01_task_model_bindings WHERE task_id = ?",
            (task.task_id,),
        )
        bound_audit = conn.execute(
            "SELECT event_id FROM audit_events "
            "WHERE event_type = 'v01.model.bound' AND entity_type = 'task' AND entity_id = ?",
            (task.task_id,),
        ).fetchone()
        assert bound_audit is not None

    audit_guarded = restarted_provider()["v01_team_task"]
    assert audit_guarded is not None
    audit_guarded_comparison = audit_guarded["final_result"]["comparison"]
    assert audit_guarded_comparison["status"] == "evidence_invalid"
    assert audit_guarded_comparison["validated"] is False
    assert "model_result" not in audit_guarded_comparison
    assert _MODEL_TEXT_CANARY not in json.dumps(
        audit_guarded,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert calls == [model_name] * 3
