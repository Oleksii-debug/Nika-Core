from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.v01_model_settings import ModelSelection, V01ModelSettings
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider


def _selection_payload(route_kind: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "schema_version": 1,
        "route_kind": route_kind,
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 30.0,
        "revision": 0,
    }
    if route_kind == "deterministic":
        return {
            **common,
            "provider_id": None,
            "model": None,
            "base_url": None,
        }
    if route_kind == "foundry_local":
        return {
            **common,
            "provider_id": "foundry-local",
            "model": "fixture-foundry-model",
            "base_url": None,
        }
    if route_kind == "ollama":
        return {
            **common,
            "provider_id": "ollama",
            "model": "fixture-ollama-model",
            "base_url": "http://localhost:11434",
        }
    raise AssertionError(route_kind)


def _bound_task(tmp_path: Path, route_kind: str) -> tuple[SQLiteStore, str, ModelSelection]:
    store = SQLiteStore(tmp_path / f"{route_kind}.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure(_selection_payload(route_kind)).status == "completed"
    payload = settings.prepare_task_payload({"command": "compare"})
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    )
    selection = settings.for_task(task.task_id)
    return store, task.task_id, selection


def _frozen_identity(store: SQLiteStore, task_id: str) -> tuple[str, str, str] | None:
    with store.connection() as conn:
        return V01PackagedTeamStateProvider._frozen_model_identity(
            conn,
            shared_task_id=task_id,
        )


def _rewrite_bound_audit(
    store: SQLiteStore,
    task_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id, payload_json FROM audit_events "
            "WHERE event_type = 'v01.model.bound' AND entity_type = 'task' AND entity_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert isinstance(payload, dict)
        mutate(payload)
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                row["event_id"],
            ),
        )
        return payload


def test_current_model_bound_audit_shape_is_accepted_for_model_route(tmp_path: Path) -> None:
    store, task_id, selection = _bound_task(tmp_path, "ollama")

    assert _frozen_identity(store, task_id) == (
        "ollama",
        "local",
        model_identity_fingerprint(selection.model),
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type = 'v01.model.bound' AND entity_type = 'task' AND entity_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
    assert payload == {
        "schema_version": 1,
        "intelligence_mode": "local_external",
        "provider_id": "ollama",
        "provider_kind": "local",
        "model_fingerprint": model_identity_fingerprint(selection.model),
    }


@pytest.mark.parametrize("hostile_schema_version", [True, 1.0])
def test_current_audit_rejects_non_integer_schema_version_after_restart(
    tmp_path: Path,
    hostile_schema_version: object,
) -> None:
    store, task_id, _ = _bound_task(tmp_path, "ollama")

    def replace_schema_version(payload: dict[str, Any]) -> None:
        payload["schema_version"] = hostile_schema_version

    _rewrite_bound_audit(store, task_id, replace_schema_version)
    restarted_store = SQLiteStore(store.path)
    with pytest.raises(ValueError, match="model binding audit differs from frozen selection"):
        _frozen_identity(restarted_store, task_id)


def test_historical_model_bound_audit_shape_remains_restart_compatible(tmp_path: Path) -> None:
    store, task_id, selection = _bound_task(tmp_path, "ollama")
    _rewrite_bound_audit(store, task_id, lambda payload: payload.pop("intelligence_mode"))

    assert _frozen_identity(store, task_id) == (
        "ollama",
        "local",
        model_identity_fingerprint(selection.model),
    )


def test_explicit_deterministic_authority_is_valid_without_model_evidence(tmp_path: Path) -> None:
    store, task_id, _ = _bound_task(tmp_path, "deterministic")

    assert _frozen_identity(store, task_id) is None
    with store.connection() as conn:
        assert (
            V01PackagedTeamStateProvider._model_evidence_required(
                conn,
                shared_task_id=task_id,
            )
            is False
        )


def test_foundry_authority_remains_model_bound(tmp_path: Path) -> None:
    store, task_id, selection = _bound_task(tmp_path, "foundry_local")

    assert _frozen_identity(store, task_id) == (
        "foundry-local",
        "local",
        model_identity_fingerprint(selection.model),
    )


@pytest.mark.parametrize("route_kind", ["deterministic", "foundry_local"])
def test_new_route_audit_cannot_downgrade_to_historical_shape(
    tmp_path: Path,
    route_kind: str,
) -> None:
    store, task_id, _ = _bound_task(tmp_path, route_kind)
    _rewrite_bound_audit(store, task_id, lambda payload: payload.pop("intelligence_mode"))

    with pytest.raises(ValueError, match="model binding audit differs from frozen selection"):
        _frozen_identity(store, task_id)


def test_current_audit_rejects_wrong_intelligence_mode(tmp_path: Path) -> None:
    store, task_id, _ = _bound_task(tmp_path, "ollama")

    def wrong_mode(payload: dict[str, Any]) -> None:
        payload["intelligence_mode"] = "api_configured"

    _rewrite_bound_audit(store, task_id, wrong_mode)
    with pytest.raises(ValueError, match="model binding audit differs from frozen selection"):
        _frozen_identity(store, task_id)


def test_current_audit_rejects_unknown_authority_fields(tmp_path: Path) -> None:
    store, task_id, _ = _bound_task(tmp_path, "ollama")

    def add_unknown(payload: dict[str, Any]) -> None:
        payload["raw_provider_state"] = "must-not-be-accepted"

    _rewrite_bound_audit(store, task_id, add_unknown)
    with pytest.raises(ValueError, match="model binding audit differs from frozen selection"):
        _frozen_identity(store, task_id)
