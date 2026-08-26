from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from nika_core.config import AppConfig
from scripts.nika_windows import build_windows_bridge

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
_FORBIDDEN_FIELDS = {
    "evidence",
    "evidence_refs",
    "credential_refs",
    "authorization_ref",
    "provider_session",
    "protected_store_handle",
}
_EXPECTED_FIELDS = {
    "title",
    "project_id",
    "goal",
    "state",
    "spec_version",
    "blocker_count",
    "status_count",
    "decision_count",
}


def _real_product_project_state(database: Path, *, create: bool) -> dict[str, object]:
    bridge, products = build_windows_bridge(AppConfig(database_path=database))
    if create:
        result = bridge.dispatch(
            {
                "request_id": "qa-product-project-create",
                "action_id": "task.create",
                "payload": {
                    "command": "Створи застосунок для доступного обліку витрат"
                },
            }
        )
        assert result["status"] == "completed", result

    response = bridge.get_state()
    assert response.get("ok") is True, response
    state = response.get("state")
    assert isinstance(state, Mapping)
    product_project = state.get("product_project")
    assert isinstance(product_project, Mapping)
    projected = dict(product_project)

    assert _EXPECTED_FIELDS.issubset(projected)
    assert _FORBIDDEN_FIELDS.isdisjoint(projected)
    assert isinstance(projected["project_id"], str)
    detail = products.inspect_project(projected["project_id"])
    assert projected["title"] == detail.summary.title
    assert projected["goal"] == detail.summary.goal
    assert projected["state"] == detail.summary.state
    assert projected["spec_version"] == detail.summary.version
    assert projected["blocker_count"] == detail.summary.blocker_count
    assert projected["status_count"] == len(detail.statuses)
    assert projected["decision_count"] == len(detail.decisions)
    return projected


def _render_real_projection(product_project: Mapping[str, object]) -> dict[str, object]:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for exact-parent ProductProject UI QA"
    project_json = json.dumps(dict(product_project), ensure_ascii=True)
    harness = f"""
const fs = require("fs");
const PROJECT = {project_json};

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
global.crypto = {{ randomUUID: () => "qa-real-bridge-request" }};
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => ({{
      ok: true,
      state: {{ tasks: [], agents: [], workspaces: [], product_project: PROJECT }},
    }}),
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
    assert lines, "Node harness produced no ProductProject rendering result"
    decoded = json.loads(lines[-1])
    assert isinstance(decoded, dict)
    return decoded


def _assert_render_matches_projection(
    rendered: Mapping[str, object], product_project: Mapping[str, object]
) -> None:
    assert rendered["ready"] == "true"
    assert rendered["empty_hidden"] is True
    assert rendered["summary_hidden"] is False
    for field in _EXPECTED_FIELDS:
        assert rendered[field] == str(product_project[field])


def test_real_packaged_bridge_projection_renders_and_survives_bridge_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "Nika Core ProductProject UI QA.db"

    first = _real_product_project_state(database, create=True)
    _assert_render_matches_projection(_render_real_projection(first), first)

    restarted = _real_product_project_state(database, create=False)
    assert restarted == first
    _assert_render_matches_projection(_render_real_projection(restarted), restarted)
