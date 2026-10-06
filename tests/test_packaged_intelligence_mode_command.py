from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.packaged_intelligence_mode import (
    PackagedIntelligenceModeCommandAdapter,
    is_packaged_intelligence_mode_command,
)
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import PackagedProductCommandRouter
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult
from nika_core.v01_model_settings import V01ModelSettings
from scripts import nika_windows

ROOT = Path(__file__).resolve().parents[1]


class _OrdinaryHandler:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary-task",
            focus_id="tasks-heading",
        )


def _composition(
    database: Path,
) -> tuple[
    SQLiteStore,
    V01ModelSettings,
    PackagedProductCommandRouter,
    _OrdinaryHandler,
]:
    store = SQLiteStore(database)
    store.initialize()
    settings = V01ModelSettings(store)
    ordinary = _OrdinaryHandler()
    adapter = PackagedIntelligenceModeCommandAdapter(settings)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
        intelligence_mode_handler=adapter.execute,
    )
    return store, settings, router, ordinary


def test_explicit_mode_namespace_reports_and_selects_deterministic_without_task_creation(
    tmp_path: Path,
) -> None:
    store, settings, router, ordinary = _composition(tmp_path / "mode.db")
    queue = TaskQueue(store)

    missing = router.create({"command": "режим інтелекту"})
    selected = router.create({"command": "режим інтелекту no_llm"})
    status = router.create({"command": "intelligence mode status"})

    assert missing.status == "completed"
    assert "не налаштовано" in missing.message
    assert selected.status == "completed"
    assert settings.snapshot() == {
        "status": "ready",
        "revision": 1,
        "intelligence_mode": "no_llm",
        "route_kind": "deterministic",
        "provider_id": None,
        "provider_kind": None,
        "model": None,
        "base_url": None,
        "timeout_seconds": 60.0,
        "private_data_allowed": True,
        "credential_configured": False,
    }
    assert status.status == "completed"
    assert "no_llm" in status.message
    assert "deterministic" in status.message
    assert ordinary.calls == []
    assert queue.list_recent(limit=10) == ()


def test_ollama_switch_is_durable_and_cannot_rebind_already_accepted_task(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mode-restart.db"
    store, settings, router, ordinary = _composition(database)

    selected = router.create(
        {
            "command": (
                "intelligence mode ollama qwen3:8b "
                "http://localhost:11434"
            )
        }
    )
    assert selected.status == "completed"

    accepted = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "accepted under ollama"}),
    )
    assert settings.for_task(accepted.task_id).route_kind == "ollama"

    changed = router.create({"command": "intelligence mode deterministic"})
    assert changed.status == "completed"
    assert settings.snapshot()["route_kind"] == "deterministic"

    restarted_store = SQLiteStore(database)
    restarted_settings = V01ModelSettings(restarted_store)

    assert restarted_settings.snapshot()["route_kind"] == "deterministic"
    frozen = restarted_settings.for_task(accepted.task_id)
    assert frozen.route_kind == "ollama"
    assert frozen.provider_id == "ollama"
    assert frozen.model == "qwen3:8b"
    assert frozen.base_url == "http://localhost:11434"
    assert frozen.private_data_allowed is True
    assert ordinary.calls == []


def test_foundry_and_api_commands_delegate_to_canonical_settings_without_secret_echo(
    tmp_path: Path,
) -> None:
    _store, settings, router, ordinary = _composition(tmp_path / "providers.db")

    foundry = router.create(
        {"command": "режим інтелекту foundry embedded-small"}
    )
    assert foundry.status == "completed"
    foundry_snapshot = settings.snapshot()
    assert foundry_snapshot["route_kind"] == "foundry_local"
    assert foundry_snapshot["provider_id"] == "foundry-local"
    assert foundry_snapshot["model"] == "embedded-small"

    api = router.create(
        {
            "command": (
                "intelligence mode api configured-api api-v1 "
                "https://api.example.test/v1 env:NIKA_TEST_REFERENCE private"
            )
        }
    )
    assert api.status == "completed"
    assert "NIKA_TEST_REFERENCE" not in api.message
    api_snapshot = settings.snapshot()
    assert api_snapshot["route_kind"] == "openai_compatible"
    assert api_snapshot["provider_id"] == "configured-api"
    assert api_snapshot["model"] == "api-v1"
    assert api_snapshot["base_url"] == "https://api.example.test/v1"
    assert api_snapshot["credential_configured"] is True
    assert api_snapshot["private_data_allowed"] is True
    assert "credential_ref" not in api_snapshot

    secret_canary = "sk-direct-command-secret-canary"
    rejected_secret = router.create(
        {
            "command": (
                "intelligence mode api configured-api api-v2 "
                f"https://api.example.test/v1 {secret_canary} private"
            )
        }
    )
    assert rejected_secret.status == "rejected"
    assert secret_canary not in rejected_secret.message
    assert settings.snapshot() == api_snapshot

    status = router.create({"command": "режим інтелекту"})
    assert status.status == "completed"
    assert "NIKA_TEST_REFERENCE" not in status.message
    assert "https://api.example.test" not in status.message
    assert ordinary.calls == []


def test_malformed_mode_command_is_rejected_without_falling_through_or_mutating_state(
    tmp_path: Path,
) -> None:
    _store, settings, router, ordinary = _composition(tmp_path / "rejected.db")

    result = router.create({"command": "intelligence mode ollama qwen3:8b"})
    controlled = router.create(
        {"command": "intelligence mode \u202e deterministic"}
    )
    embedded_control = router.create(
        {"command": "intelli\u202egence mode deterministic"}
    )

    assert result.status == "rejected"
    assert "loopback-url" in result.message
    assert result.focus_id == "command-input"
    assert controlled.status == "rejected"
    assert embedded_control.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}
    assert ordinary.calls == []
    assert router.active_project_id is None


def test_mode_namespace_is_exact_and_ordinary_commands_keep_existing_route(
    tmp_path: Path,
) -> None:
    _store, _settings, router, ordinary = _composition(tmp_path / "namespace.db")

    assert is_packaged_intelligence_mode_command("intelligence mode") is True
    assert is_packaged_intelligence_mode_command("режим інтелекту ollama x y") is True
    assert is_packaged_intelligence_mode_command("intelligence model ollama x y") is False
    assert is_packaged_intelligence_mode_command("режим інтелектуальний") is False

    ordinary_result = router.create({"command": "Порахуй кількість слів у цьому тексті"})

    assert ordinary_result.message == "ordinary-task"
    assert len(ordinary.calls) == 1


def test_real_windows_bridge_executes_mode_command_without_creating_task(
    tmp_path: Path,
) -> None:
    database = tmp_path / "Дані Nika" / "ніка.db"
    config = AppConfig(database_path=database)

    bridge, _products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    result = bridge.dispatch(
        {
            "request_id": "direct-mode-command",
            "action_id": "task.create",
            "payload": {
                "command": (
                    "режим інтелекту ollama qwen3:8b "
                    "http://localhost:11434"
                )
            },
        }
    )

    assert result["request_id"] == "direct-mode-command"
    assert result["status"] == "completed"
    assert result["focus_id"] == "command-input"
    state = bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["v01_model_settings"]["route_kind"] == "ollama"
    assert state["state"]["v01_model_settings"]["model"] == "qwen3:8b"
    assert TaskQueue(SQLiteStore(database)).list_recent(limit=10) == ()


def test_packaged_windows_composition_exposes_accessible_mode_command_help() -> None:
    script = (ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")
    html = (ROOT / "src" / "nika_core" / "ui" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "PackagedIntelligenceModeCommandAdapter" in script
    assert "intelligence_mode_handler=intelligence_mode_commands.execute" in script
    assert 'aria-describedby="execution-mode command-intelligence-help"' in html
    assert 'id="command-intelligence-help"' in html
    assert "режим інтелекту deterministic" in html
    assert "Уже прийняті завдання не перемикаються." in html
