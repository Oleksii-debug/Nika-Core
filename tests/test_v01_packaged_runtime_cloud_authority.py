from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_cloud_model_permission import V01CloudModelPermissionService
from nika_core.v01_model_settings import V01BoundModelRuntimeFactory, V01ModelSettings
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime


def _store(tmp_path: Path) -> tuple[SQLiteStore, AppConfig]:
    database = tmp_path / "nika.db"
    config = AppConfig(database_path=database)
    store = SQLiteStore(database)
    store.initialize()
    return store, config


def test_packaged_runtime_uses_shared_settings_and_cloud_authority_pair(
    tmp_path: Path,
) -> None:
    store, config = _store(tmp_path)
    settings = V01ModelSettings(store)
    permissions = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
    )

    runtime = V01PackagedThreeAgentRuntime(
        store=store,
        config=config,
        model_settings=settings,
        cloud_effect_authorizer=permissions.cloud_effect_authorizer,
        cloud_execution_authority_resolver=permissions.execution_authority_for_task,
    )

    assert runtime._model_settings is settings
    assert runtime._model_factory._settings is settings
    assert (
        runtime._model_factory._cloud_effect_authorizer
        is permissions.cloud_effect_authorizer
    )
    resolver = runtime._model_factory._cloud_execution_authority_resolver
    assert resolver is not None
    assert resolver.__self__ is permissions


def test_packaged_runtime_rejects_duplicate_cloud_authority_configuration(
    tmp_path: Path,
) -> None:
    store, config = _store(tmp_path)
    settings = V01ModelSettings(store)
    permissions = V01CloudModelPermissionService(
        store=store,
        settings=settings,
        confirm=lambda _request: True,
    )
    custom_factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=V01PackagedThreeAgentRuntime(
            store=store,
            config=config,
            model_settings=settings,
        )._definitions,
        settings=settings,
    )

    with pytest.raises(TypeError, match="either the packaged runtime"):
        V01PackagedThreeAgentRuntime(
            store=store,
            config=config,
            model_settings=settings,
            model_runtime_factory=custom_factory,
            cloud_effect_authorizer=permissions.cloud_effect_authorizer,
            cloud_execution_authority_resolver=permissions.execution_authority_for_task,
        )
