from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.labels: dict[str, str] = {}
        self.buttons: dict[str, str] = {}
        self.options: dict[str, str] = {}
        self._label: str | None = None
        self._button: str | None = None
        self._profile = False
        self._option: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if "id" in values:
            self.nodes[str(values["id"])] = (tag, values)
        if tag == "label":
            self._label = values.get("for")
        if tag == "button":
            self._button = values.get("data-action-id")
        if tag == "select" and values.get("id") == "resource-profile":
            self._profile = True
        if tag == "option" and self._profile:
            self._option = values.get("value")

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._label = None
        if tag == "button":
            self._button = None
        if tag == "option":
            self._option = None
        if tag == "select":
            self._profile = False

    def handle_data(self, data: str) -> None:
        if self._label:
            self.labels[self._label] = self.labels.get(self._label, "") + data
        if self._button:
            self.buttons[self._button] = self.buttons.get(self._button, "") + data
        if self._option:
            self.options[self._option] = self.options.get(self._option, "") + data


def test_resource_and_schedule_controls_have_native_labels_and_ukrainian_names() -> None:
    controls = _Controls()
    controls.feed((ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8"))
    labels = {
        "resource-profile": "Режим роботи",
        "schedule-delay": "Затримка перед запуском, секунд",
        "schedule-retry": "Повторити перевірку ресурсів через, секунд",
        "schedule-window": "Крайній час запуску — секунд від цього моменту",
        "schedule-task": "Збережений запланований запуск",
    }
    for control_id, label in labels.items():
        assert controls.labels[control_id].strip() == label
        assert controls.nodes[control_id][0] in {"input", "select"}
    assert controls.options == {
        "normal": "Звичайний",
        "economy": "Економний",
        "night_batch": "Нічна обробка",
        "low_memory": "Мало пам’яті",
    }
    expected_buttons = {
        "settings.resources.configure": "Зберегти профіль ресурсів",
        "schedule.create": "Запланувати запуск",
        "schedule.pause": "Призупинити запланований запуск",
        "schedule.resume": "Відновити запланований запуск",
        "schedule.cancel": "Скасувати запланований запуск",
    }
    for action, label in expected_buttons.items():
        assert controls.buttons[action].strip() == label
    for control_id in ("schedule-delay", "schedule-retry", "schedule-window"):
        assert controls.nodes[control_id][1]["type"] == "number"
        assert controls.nodes[control_id][1]["step"] == "1"
    profile_description = controls.nodes["resource-profile"][1]["aria-describedby"]
    assert profile_description and "resource-profile-status" in profile_description.split()
    assert controls.nodes["resource-profile-heading"][1]["tabindex"] == "-1"
    assert controls.nodes["schedule-heading"][1]["tabindex"] == "-1"


def test_actual_resource_schedule_renderer_handles_writes_races_failures_and_focus() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the actual UI JavaScript")
    result = subprocess.run(
        [node, str(ROOT / "tests/js/resource_schedule_harness.cjs"),
         str(ROOT / "src/nika_core/ui/web/app.js")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: resource/schedule renderer" in result.stdout
