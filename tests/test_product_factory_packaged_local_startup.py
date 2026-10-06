from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartupError,
    build_packaged_local_product_factory_program,
    decode_packaged_local_product_factory_startup,
)
from nika_core.v01_model_settings import V01ModelSettings
from scripts import nika_windows


def _git(root: pathlib.Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    result = subprocess.run(
        (executable, *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path) -> pathlib.Path:
    repository = tmp_path / "репозиторій"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Nika Test")
    _git(repository, "config", "user.email", "nika@example.invalid")
    (repository / "app.py").write_text("print('old')\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    _git(repository, "commit", "-m", "base")
    return repository.resolve()


def _startup_json(
    tmp_path: pathlib.Path,
    repository: pathlib.Path,
    *,
    executable: str,
) -> str:
    workspace = tmp_path / "factory jobs"
    workspace.mkdir()
    return json.dumps(
        {
            "schema": "nika.product-factory.local-startup.v1",
            "workspace_parent": str(workspace.resolve()),
            "repositories": {"repo-1": str(repository.resolve())},
            "allowed_executables": [executable],
            "resource_budget": {
                "timeout_seconds": 30,
                "max_output_bytes": 1024 * 1024,
                "max_changed_files": 20,
            },
            "lease_seconds": 300,
            "git_executable": executable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configure_ollama(settings: V01ModelSettings) -> None:
    result = settings.configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"


def test_startup_decoder_binds_only_explicit_local_host_authority(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(tmp_path, repository, executable=str(pathlib.Path(executable).resolve()))

    startup = decode_packaged_local_product_factory_startup(raw)

    assert startup is not None
    assert startup.repositories == {"repo-1": repository}
    assert startup.workspace_parent.name == "factory jobs"
    assert startup.policy.lease_seconds == 300
    assert startup.policy.resource_budget.max_changed_files == 20
    assert startup.git_executable == pathlib.Path(executable).resolve()


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema":"nika.product-factory.local-startup.v1","schema":"duplicate"}',
        '{"schema":"nika.product-factory.local-startup.v1"}',
        " []",
        '{"schema":NaN}',
    ],
)
def test_startup_decoder_rejects_ambiguous_or_incomplete_json(raw: str) -> None:
    with pytest.raises(PackagedLocalProductFactoryStartupError):
        decode_packaged_local_product_factory_startup(raw)


def test_startup_decoder_rejects_relative_repository_authority(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = json.loads(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    raw["repositories"]["repo-1"] = "relative/repository"

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="repository repo-1 path must be absolute",
    ):
        decode_packaged_local_product_factory_startup(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )


def test_program_composition_reuses_persisted_ollama_and_canonical_local_worker(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    startup = decode_packaged_local_product_factory_startup(raw)
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = V01ModelSettings(store)
    _configure_ollama(settings)

    program = build_packaged_local_product_factory_program(
        store,
        settings=settings,
        startup=startup,
    )

    assert program.multi_repository_host.store is store
    assert program.multi_repository_host._program is program.host
    assert program.host.worker.worker is program.worker
    assert program.worker.repositories == {"repo-1": repository}
    assert program.worker.planner is not None
    assert program.worker.planner.provider_id == "ollama"
    assert program.worker.planner.provider_kind.value == "local"
    assert program.worker.planner.model == "qwen3:8b"
    assert program.worker.planner.gateway.providers() == ("ollama",)


def test_program_composition_fails_closed_for_deterministic_route_before_worker(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "deterministic",
            "provider_id": None,
            "model": None,
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="requires the persisted Ollama LOCAL route",
    ):
        build_packaged_local_product_factory_program(
            store,
            settings=settings,
            startup=startup,
        )


def test_program_composition_fails_closed_when_model_selection_missing(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    startup = decode_packaged_local_product_factory_startup(
        _startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        )
    )
    assert startup is not None
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    with pytest.raises(
        PackagedLocalProductFactoryStartupError,
        match="select a local Ollama model",
    ):
        build_packaged_local_product_factory_program(
            store,
            settings=V01ModelSettings(store),
            startup=startup,
        )


def test_app_config_admits_bounded_startup_authority_without_interpreting_plan_data(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )

    config = AppConfig(
        database_path=(tmp_path / "configured.db").resolve(),
        product_factory_local_startup_json=raw,
    )

    assert config.product_factory_local_startup_json == raw


def test_app_config_rejects_unbounded_startup_authority_before_runtime(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(ValueError, match="exceeds the size limit"):
        AppConfig(
            database_path=(tmp_path / "configured.db").resolve(),
            product_factory_local_startup_json="{" + ("x" * (64 * 1024)) + "}",
        )


def test_windows_bridge_auto_composes_real_local_execution_host_from_config(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    database = (tmp_path / "ніка.db").resolve()
    raw = _startup_json(
        tmp_path,
        repository,
        executable=str(pathlib.Path(executable).resolve()),
    )
    seed_store = SQLiteStore(database)
    seed_store.initialize()
    _configure_ollama(V01ModelSettings(seed_store))
    config = AppConfig(
        database_path=database,
        product_factory_local_startup_json=raw,
    )
    cleanup: list[object] = []

    try:
        bridge, products = nika_windows.build_windows_bridge(
            config,
            start_startup_recovery=False,
            register_cleanup=cleanup.append,
        )

        assert bridge is not None
        assert products is not None
        state = bridge.get_state()
        assert state["ok"] is True
        assert state["state"]["product_factory_execution_plan"] == {
            "status": "missing",
            "loaded": False,
            "project_id": None,
            "message": "JSON-план виконання Product Factory ще не завантажено.",
        }
    finally:
        for callback in reversed(cleanup):
            callback()


def test_windows_bridge_rejects_two_product_factory_host_authorities(
    tmp_path: pathlib.Path,
) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    repository = _repository(tmp_path)
    config = AppConfig(
        database_path=(tmp_path / "ніка.db").resolve(),
        product_factory_local_startup_json=_startup_json(
            tmp_path,
            repository,
            executable=str(pathlib.Path(executable).resolve()),
        ),
    )

    with pytest.raises(ValueError, match="conflicts"):
        nika_windows.build_windows_bridge(
            config,
            start_startup_recovery=False,
            product_factory_execution_host=object(),  # type: ignore[arg-type]
        )
