from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, Thread

import pytest

import nika_core.product_factory_packaged_execution_plan_file as plan_file_module
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_packaged_execution_plan_file import (
    PackagedExecutionPlanFileError,
    PackagedProductFactoryExecutionPlanFileSource,
)
from scripts import nika_windows

ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ID = "product-" + "a" * 64


def _claim(project_id: str = _PROJECT_ID) -> dict[str, object]:
    return {
        "schema": "nika-packaged-product-factory-execution-plan-v1",
        "project_id": project_id,
        "expected_spec_version": 1,
        "expected_row_version": 0,
        "graph_version": 1,
        "repositories": [
            {
                "repository_id": "repo-core",
                "provider": "github",
                "locator": "Oleksii-debug/Nika-Core",
                "default_branch": "main",
                "credential_ref": None,
                "case_sensitive_paths": True,
            }
        ],
        "components": [
            {
                "component_id": "core",
                "repository_id": "repo-core",
                "paths": ["src/nika_core"],
                "dependencies": [],
                "build_commands": [],
                "test_commands": [["python", "-m", "pytest", "tests"]],
                "release_identity": None,
            }
        ],
        "base_shas": {"repo-core": "c" * 40},
        "component_goals": {
            "core": "Implement the explicitly accepted ProductProject work"
        },
        "permission_ceiling": ["read_source", "write_source", "run_tests"],
    }


def _write_plan(path: Path, *, project_id: str = _PROJECT_ID) -> Path:
    path.write_bytes(
        json.dumps(
            _claim(project_id),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return path


def test_file_source_loads_and_resolves_exact_project_without_projecting_path(
    tmp_path: Path,
) -> None:
    path = _write_plan(tmp_path / "план виконання.json")
    source = PackagedProductFactoryExecutionPlanFileSource()

    result = source.load({"path": str(path.resolve())})
    plan = source.resolve(_PROJECT_ID)
    snapshot = source.snapshot()

    assert result.status == "completed"
    assert result.focus_id == "product-factory-execution-plan-path"
    assert plan.project_id == _PROJECT_ID
    assert plan.base_shas == {"repo-core": "c" * 40}
    assert snapshot == {
        "status": "loaded",
        "loaded": True,
        "project_id": _PROJECT_ID,
        "message": (
            "JSON-план виконання Product Factory завантажено для "
            f"{_PROJECT_ID}."
        ),
    }
    assert "path" not in snapshot
    assert str(path) not in result.message


def test_failed_reload_clears_previously_loaded_authority(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "valid.json")
    source = PackagedProductFactoryExecutionPlanFileSource()
    assert source.load({"path": str(path.resolve())}).status == "completed"

    rejected = source.load(
        {
            "path": str(path.resolve()),
            "unexpected": "must-clear-prior-plan",
        }
    )

    assert rejected.status == "rejected"
    assert source.snapshot()["status"] == "missing"
    with pytest.raises(PackagedExecutionPlanFileError, match="no packaged"):
        source.resolve(_PROJECT_ID)


def test_invalid_json_reload_clears_previously_loaded_authority(tmp_path: Path) -> None:
    valid = _write_plan(tmp_path / "valid.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema":"wrong"}', encoding="utf-8")
    source = PackagedProductFactoryExecutionPlanFileSource()
    assert source.load({"path": str(valid.resolve())}).status == "completed"

    rejected = source.load({"path": str(invalid.resolve())})

    assert rejected.status == "rejected"
    assert source.snapshot()["loaded"] is False
    with pytest.raises(PackagedExecutionPlanFileError, match="no packaged"):
        source.resolve(_PROJECT_ID)


def test_newer_load_wins_when_an_older_file_read_finishes_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _write_plan(tmp_path / "first.json")
    newer_project = "product-" + "d" * 64
    second = _write_plan(tmp_path / "second.json", project_id=newer_project)
    source = PackagedProductFactoryExecutionPlanFileSource()
    first_started = Event()
    release_first = Event()
    original = plan_file_module._read_stable_plan_bytes

    def delayed_read(path: Path) -> bytes:
        if path == first.resolve():
            first_started.set()
            if not release_first.wait(timeout=5):
                raise RuntimeError("timed out waiting to finish stale plan read")
        return original(path)

    monkeypatch.setattr(plan_file_module, "_read_stable_plan_bytes", delayed_read)
    results: dict[str, object] = {}

    def load_first() -> None:
        results["first"] = source.load({"path": str(first.resolve())})

    worker = Thread(target=load_first, daemon=True)
    worker.start()
    assert first_started.wait(timeout=5)

    newer_result = source.load({"path": str(second.resolve())})
    release_first.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert newer_result.status == "completed"
    stale_result = results["first"]
    assert getattr(stale_result, "status") == "rejected"
    assert source.resolve(newer_project).project_id == newer_project
    with pytest.raises(PackagedExecutionPlanFileError, match="another ProductProject"):
        source.resolve(_PROJECT_ID)


def test_file_source_rejects_plan_for_other_project_at_resolution(tmp_path: Path) -> None:
    other = "product-" + "b" * 64
    path = _write_plan(tmp_path / "other.json", project_id=other)
    source = PackagedProductFactoryExecutionPlanFileSource()
    assert source.load({"path": str(path.resolve())}).status == "completed"

    with pytest.raises(PackagedExecutionPlanFileError, match="another ProductProject"):
        source.resolve(_PROJECT_ID)


def test_file_source_rejects_relative_path_before_read(tmp_path: Path) -> None:
    source = PackagedProductFactoryExecutionPlanFileSource()

    result = source.load({"path": "relative-plan.json"})

    assert result.status == "rejected"
    assert result.focus_id == "product-factory-execution-plan-path"
    assert source.snapshot()["status"] == "missing"


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow contract")
def test_file_source_fails_closed_without_posix_nofollow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_plan(tmp_path / "plan.json")
    monkeypatch.delattr(plan_file_module.os, "O_NOFOLLOW", raising=False)
    source = PackagedProductFactoryExecutionPlanFileSource()

    result = source.load({"path": str(path.resolve())})

    assert result.status == "failed"
    assert source.snapshot()["status"] == "missing"


def test_file_source_rejects_hardlinked_plan(tmp_path: Path) -> None:
    path = _write_plan(tmp_path / "plan.json")
    alias = tmp_path / "plan-alias.json"
    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable in test environment: {type(exc).__name__}")
    source = PackagedProductFactoryExecutionPlanFileSource()

    result = source.load({"path": str(path.resolve())})

    assert result.status == "failed"
    assert source.snapshot()["status"] == "missing"


def test_file_source_rejects_mutation_during_held_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_plan(tmp_path / "mutating.json")
    source = PackagedProductFactoryExecutionPlanFileSource()
    original = plan_file_module._read_held_bytes
    calls = 0

    def mutating_read(descriptor: int) -> bytes:
        nonlocal calls
        payload = original(descriptor)
        calls += 1
        if calls == 1:
            path.write_bytes(b" " * len(payload))
        return payload

    monkeypatch.setattr(plan_file_module, "_read_held_bytes", mutating_read)

    result = source.load({"path": str(path.resolve())})

    assert result.status == "failed"
    assert calls >= 1
    assert source.snapshot()["status"] == "missing"


def test_file_source_rejects_oversized_plan_before_json_decode(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    source = PackagedProductFactoryExecutionPlanFileSource()

    result = source.load({"path": str(path.resolve())})

    assert result.status == "failed"
    assert source.snapshot()["status"] == "missing"


def test_accessible_execution_plan_file_controls_and_bridge_contract() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    script = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    actions = {
        action.action_id: action
        for action in build_default_action_registry().all()
    }

    action = actions["product.factory.execution_plan.load"]
    assert action.label == "Завантажити план виконання Product Factory"
    assert action.category == "Product Factory"
    assert action.default_binding is None

    assert 'id="product-factory-execution-plan-heading"' in html
    assert (
        '<label for="product-factory-execution-plan-path">'
        in html
    )
    assert 'id="product-factory-execution-plan-path"' in html
    assert 'type="text"' in html
    assert (
        'aria-describedby="product-factory-execution-plan-help '
        'product-factory-execution-plan-status"\n          disabled'
        in html
    )
    assert (
        'aria-describedby="product-factory-execution-plan-help '
        'product-factory-execution-plan-status"'
        in html
    )
    assert 'data-action-id="product.factory.execution_plan.load"' in html
    assert 'data-error-focus-target="product-factory-execution-plan-path"' in html
    assert (
        'data-error-focus-target="product-factory-execution-plan-path"\n            disabled'
        in html
    )

    assert (
        'if (actionId === "product.factory.execution_plan.load")'
        in app
    )
    assert (
        'payload.path = productFactoryExecutionPlanPath?.value ?? "";'
        in app
    )
    assert (
        "renderProductFactoryExecutionPlan("
        in app
    )
    assert (
        "state.product_factory_execution_plan ?? null"
        in app
    )

    assert (
        "PackagedProductFactoryExecutionPlanFileSource()"
        in script
    )
    assert (
        "product_factory_execution_host is not None\n"
        "            and product_factory_execution_plan_resolver is None"
        in script
    )
    assert "execution_plan_resolver = product_factory_execution_plan_resolver" in script
    assert "execution_plan_resolver = product_factory_execution_plan_files.resolve" in script
    assert (
        '"product.factory.execution_plan.load": '
        "load_product_factory_execution_plan"
        in script
    )
    assert (
        'state["product_factory_execution_plan"] = ('
        in script
    )

class _UnusedProgramWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str):
        return None

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


def _plan_file_command(path: Path, request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "action_id": "product.factory.execution_plan.load",
        "payload": {"path": str(path)},
    }


def test_windows_bridge_rejects_plan_file_when_execution_host_is_unconfigured(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    bridge, _products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )

    state = bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["product_factory_execution_plan"] is None

    result = bridge.dispatch(
        _plan_file_command(tmp_path / "must-not-be-read.json", "plan-no-host")
    )
    assert result["status"] == "rejected"
    assert "недоступне" in result["message"]
    assert result["focus_id"] == "product-factory-execution-plan-path"


def test_windows_bridge_rejects_plan_file_when_custom_resolver_owns_authority(
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "Ніка дані" / "nika.db")
    host_store = SQLiteStore(config.database_path)
    host_store.initialize()
    host = MultiRepositoryProductFactoryHost(host_store, _UnusedProgramWorker())

    def custom_resolver(_project_id: str):
        raise AssertionError("custom resolver must not run during file admission")

    bridge, _products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
        product_factory_execution_host=host,
        product_factory_execution_plan_resolver=custom_resolver,
    )

    state = bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["product_factory_execution_plan"] is None

    result = bridge.dispatch(
        _plan_file_command(tmp_path / "must-not-be-read.json", "plan-custom-resolver")
    )
    assert result["status"] == "rejected"
    assert "недоступне" in result["message"]
    assert result["focus_id"] == "product-factory-execution-plan-path"
