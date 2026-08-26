from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.product_factory_packaged_journey import product_project_identity
from scripts.nika_windows import build_windows_bridge

_ROOT = Path(__file__).parents[1]
_APP_JS = _ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
_NODE = shutil.which("node")
_FORBIDDEN_PROJECT_FIELDS = {
    "evidence_refs",
    "credential_refs",
    "authorization_ref",
    "provider_session",
    "protected_store_handle",
}


def _render_live_response(response: Mapping[str, Any]) -> dict[str, object]:
    node = _NODE
    if node is None:
        raise RuntimeError("Node.js is required for the live bridge-to-renderer oracle")
    response_json = json.dumps(response, ensure_ascii=True)
    harness = f"""
const fs = require("fs");
const RESPONSE = {response_json};

class Element {{}}
class HTMLElement extends Element {{
  constructor(id = "") {{
    super();
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.dataset = {{}};
    this.attributes = {{}};
    this.children = [];
    this.isContentEditable = false;
  }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  addEventListener() {{}}
  replaceChildren(...children) {{ this.children = children; }}
  appendChild(child) {{ this.children.push(child); return child; }}
  append(...children) {{ this.children.push(...children); }}
  focus() {{ document.activeElement = this; }}
  matches() {{ return false; }}
  closest() {{ return null; }}
}}

global.Element = Element;
global.HTMLElement = HTMLElement;

const elements = Object.create(null);
function element(id) {{
  if (!elements[id]) elements[id] = new HTMLElement(id);
  return elements[id];
}}

global.document = {{
  activeElement: null,
  documentElement: new HTMLElement("documentElement"),
  getElementById: element,
  createElement: () => new HTMLElement(),
  createTextNode: (value) => {{
    const node = new HTMLElement();
    node.textContent = String(value);
    return node;
  }},
  addEventListener: () => {{}},
}};
global.window = {{ addEventListener: () => {{}} }};
global.crypto = {{ randomUUID: () => "qa-live-bridge-request" }};
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => RESPONSE,
    export_keymap: async () => ({{ ok: true, message: "ok", data: "{{}}" }}),
    import_keymap: async () => ({{ ok: true, message: "ok" }}),
    set_binding: async () => ({{ ok: true, message: "ok" }}),
    restore_default: async () => ({{ ok: true, message: "ok" }}),
    dispatch: async () => ({{ status: "completed", message: "ok" }}),
  }},
}};

eval(fs.readFileSync(process.argv[1], "utf8"));

setTimeout(() => {{
  console.log(JSON.stringify({{
    empty_hidden: element("product-project-empty").hidden,
    summary_hidden: element("product-project-summary").hidden,
    title: element("product-project-title").textContent,
    project_id: element("product-project-id").textContent,
    goal: element("product-project-goal").textContent,
    state: element("product-project-state").textContent,
    spec_version: element("product-project-spec-version").textContent,
    blocker_count: element("product-project-blocker-count").textContent,
    status_count: element("product-project-status-count").textContent,
    decision_count: element("product-project-decision-count").textContent,
    ready: document.documentElement.dataset.nikaReady || null,
  }}));
}}, 50);
"""
    result = subprocess.run(
        (node, "-e", harness, str(_APP_JS)),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "Node harness produced no live ProductProject rendering result"
    decoded = json.loads(lines[-1])
    assert isinstance(decoded, dict)
    return decoded


def _require_product_project(response: Mapping[str, Any]) -> Mapping[str, Any]:
    assert response.get("ok") is True
    state = response.get("state")
    assert isinstance(state, Mapping)
    project = state.get("product_project")
    assert isinstance(project, Mapping)
    return project


def _create_and_restart(
    tmp_path: Path,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    database = tmp_path / "live ProductProject bridge.db"
    config = AppConfig(database_path=database)
    goal = "Створи застосунок для доступного обліку витрат"
    project_id = product_project_identity(goal)

    first_bridge, first_products = build_windows_bridge(config)
    initial = first_bridge.get_state()
    assert initial.get("ok") is True
    assert initial.get("state", {}).get("product_project") is None

    created = first_bridge.dispatch(
        {
            "request_id": "qa-live-create",
            "action_id": "task.create",
            "payload": {"command": goal},
        }
    )
    assert created["status"] == "completed"
    assert first_products.inspect_project(project_id).summary.goal == goal
    first_response = first_bridge.get_state()

    restarted_bridge, restarted_products = build_windows_bridge(config)
    restarted_response = restarted_bridge.get_state()
    assert restarted_products.inspect_project(project_id).summary.goal == goal
    return goal, project_id, initial, first_response | {"restart": restarted_response}


def test_real_packaged_bridge_exposes_bounded_projection_after_restart(tmp_path: Path) -> None:
    goal, project_id, _initial, responses = _create_and_restart(tmp_path)
    first_response = {key: value for key, value in responses.items() if key != "restart"}
    restarted_response = responses["restart"]
    assert isinstance(restarted_response, Mapping)

    first_project = _require_product_project(first_response)
    restarted_project = _require_product_project(restarted_response)

    assert restarted_project == first_project
    assert first_project["project_id"] == project_id
    assert first_project["title"] == goal
    assert first_project["goal"] == goal
    assert first_project["state"] == "active"
    assert first_project["spec_version"] == 1
    assert first_project["blocker_count"] == 0
    assert first_project["status_count"] == 0
    assert first_project["decision_count"] == 0
    assert set(first_project).isdisjoint(_FORBIDDEN_PROJECT_FIELDS)


def test_exact_live_bridge_responses_render_empty_then_current_state(tmp_path: Path) -> None:
    if _NODE is None:
        pytest.skip("Node.js is required for the live bridge-to-renderer oracle")

    goal, project_id, initial, responses = _create_and_restart(tmp_path)
    restarted_response = responses["restart"]
    assert isinstance(restarted_response, Mapping)

    rendered_initial = _render_live_response(initial)
    assert rendered_initial["empty_hidden"] is False
    assert rendered_initial["summary_hidden"] is True
    assert rendered_initial["ready"] == "true"

    rendered = _render_live_response(restarted_response)
    assert rendered == {
        "empty_hidden": True,
        "summary_hidden": False,
        "title": goal,
        "project_id": project_id,
        "goal": goal,
        "state": "active",
        "spec_version": "1",
        "blocker_count": "0",
        "status_count": "0",
        "decision_count": "0",
        "ready": "true",
    }
