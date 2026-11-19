from __future__ import annotations

import json
from pathlib import Path

from nika_core.packaged_resource_schedule_proof import run_resource_schedule_proof
from scripts.nika_windows import build_windows_bridge


def test_real_builder_proof_uses_private_fixtures_without_reading_environment_database(
    tmp_path: Path, monkeypatch,
) -> None:
    user_database = tmp_path / 'Не відкривати приватну базу.db'
    monkeypatch.setenv('NIKA_DB_PATH', str(user_database))
    result = run_resource_schedule_proof(build_windows_bridge)
    assert not user_database.exists()
    assert result['actual_builder'] is True
    assert result['profile_persisted_after_restart'] is True
    assert result['scheduled_runtime_completed'] is True
    assert result['scheduled_running_event_count'] == 1
    assert result['completed_task_not_replayed'] is True
    assert result['pending_pause_resume_cancel'] is True
    assert result['cancelled_running_event_count'] == 0
    assert result['human_tested'] is False
    assert result['nvda_verified'] is False
    assert result['production_release_ready'] is False
    assert str(tmp_path) not in json.dumps(result)


def test_real_proof_closes_previous_bridge_and_removes_private_fixture_on_restart_failure() -> None:
    import pytest

    configs = []
    bridges = []

    def restart_failure(config):
        configs.append(config)
        if len(configs) == 2:
            assert not bridges[0]._finalizer.alive
            raise RuntimeError('controlled restart failure')
        bridge, products = build_windows_bridge(config)
        bridges.append(bridge)
        return bridge, products

    with pytest.raises(RuntimeError, match='controlled restart failure'):
        run_resource_schedule_proof(restart_failure)
    assert len(configs) == 2
    assert not configs[0].database_path.parent.exists()
    assert all(not bridge._finalizer.alive for bridge in bridges)
