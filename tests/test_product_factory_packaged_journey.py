from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult

ROOT = Path(__file__).resolve().parents[1]


class OrdinaryHandler:
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


def _router(
    path: Path,
) -> tuple[PackagedProductCommandRouter, ProductProjectRepository, OrdinaryHandler]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    ordinary = OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
    )
    return router, repository, ordinary


def test_product_command_creates_durable_product_project_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "Nika product journey.db"
    command = (
        "Створи застосунок для керування витратами малого бізнесу"
    )
    project_id = product_project_identity(command)

    router, repository, ordinary = _router(database)
    first = router.create({"command": command})
    project = repository.get(project_id)

    assert first.status == "completed"
    assert project.project_id == project_id
    assert project.spec.goal == command
    assert project.spec_version == 1
    assert ordinary.calls == []

    restarted, restarted_repository, restarted_ordinary = _router(database)
    second = restarted.create({"command": command})
    replayed = restarted_repository.get(project_id)

    assert second.status == "completed"
    assert replayed == project
    assert replayed.spec_version == 1
    assert restarted_ordinary.calls == []


def test_ordinary_agent_command_stays_on_existing_desktop_boundary(tmp_path: Path) -> None:
    router, repository, ordinary = _router(tmp_path / "ordinary.db")

    ordinary_command = "Порахуй кількість слів у цьому тексті"
    result = router.create({"command": ordinary_command})

    assert result.message == "ordinary-task"
    assert len(ordinary.calls) == 1
    with pytest.raises(KeyError):
        repository.get(product_project_identity(ordinary_command))


def test_ambiguous_product_and_toolsmith_command_fails_closed(tmp_path: Path) -> None:
    router, _, ordinary = _router(tmp_path / "ambiguous.db")

    with pytest.raises(PackagedProductJourneyError, match="одночасно"):
        router.create(
            {"command": "Створи застосунок і додай потрібний інструмент plugin"}
        )

    assert ordinary.calls == []


def test_toolsmith_command_does_not_silently_enter_agent_or_product_route(tmp_path: Path) -> None:
    router, _, ordinary = _router(tmp_path / "toolsmith.db")

    with pytest.raises(PackagedProductJourneyError, match="Toolsmith"):
        router.create(
            {"command": "Додай потрібний інструмент для конвертації файлів"}
        )

    assert ordinary.calls == []


def test_product_identity_is_whitespace_stable_and_goal_sensitive() -> None:
    assert product_project_identity("Create   product app") == product_project_identity(
        "Create product app"
    )
    assert product_project_identity("Create product app") != product_project_identity(
        "Create product service"
    )


def test_real_windows_composition_root_no_longer_routes_task_create_directly_to_backend() -> None:
    source = (ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")

    assert "route_command" in source
    assert "ProductProjectCommandService" in source
    assert "PackagedProductCommandRouter" in source
    assert '"task.create": backend.create_task' not in source
    assert '"task.create": product_router.create' in source


def test_headless_pf11_composition_proof_survives_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "packaged proof.db"
    env = dict(os.environ)
    env["NIKA_DB_PATH"] = str(database)
    command = [sys.executable, "scripts/nika_windows.py", "--pf11-proof"]

    first = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    first_payload = json.loads(first.stdout.strip())
    second_payload = json.loads(second.stdout.strip())
    assert first_payload == second_payload
    assert first_payload["route"] == "product_project"
    assert first_payload["spec_version"] == 1
    assert first_payload["human_tested"] is False
    assert first_payload["nvda_verified"] is False
    assert first_payload["production_release_ready"] is False


def test_release_builder_records_packaged_pf11_restart_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.m11_release import prove_packaged_product_journey

    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    executable = bundle / "NikaCore.exe"
    executable.write_bytes(b"fake-executable")
    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_run(argv, *, check, env, timeout):
        del check, timeout
        arguments = tuple(str(item) for item in argv)
        output_index = arguments.index("--pf11-proof-output") + 1
        output = Path(arguments[output_index])
        output.write_text(
            json.dumps(
                {
                    "route": "product_project",
                    "project_id": "product-" + "a" * 64,
                    "spec_version": 1,
                    "state": "active",
                    "human_tested": False,
                    "nvda_verified": False,
                    "production_release_ready": False,
                }
            ),
            encoding="utf-8",
        )
        calls.append((arguments, env["NIKA_DB_PATH"]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("scripts.m11_release.subprocess.run", fake_run)
    target = prove_packaged_product_journey(bundle, source_sha="1" * 40)
    payload = json.loads(target.read_text(encoding="utf-8"))
    from nika_core.packaging.release import build_release_manifest

    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.0.2",
        source_sha="1" * 40,
    )

    assert target.name == "pf11-packaged-product-journey.json"
    assert any(item.path == target.name for item in manifest.files)
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert payload["source_sha"] == "1" * 40
    assert payload["packaged_executable_proven"] is True
    assert payload["restart_replay_proven"] is True
    assert payload["human_tested"] is False
    assert payload["nvda_verified"] is False
    assert payload["production_release_ready"] is False
