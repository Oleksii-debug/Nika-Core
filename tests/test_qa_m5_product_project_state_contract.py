from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_APP_JS = Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web" / "app.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None,
    reason="Node.js is required for this JS behavior oracle",
)


def _render_snapshot(
    product_project: object,
    *,
    fail_refresh_after_ready: bool = False,
) -> dict[str, object]:
    project_json = json.dumps(product_project, ensure_ascii=True)
    fail_refresh_json = json.dumps(fail_refresh_after_ready)
    harness = f"""
const fs = require("fs");
const PROJECT = {project_json};
const FAIL_REFRESH_AFTER_READY = {fail_refresh_json};

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
  closest(selector) {{
    if (selector === "[data-action-id]" && this.dataset.actionId) return this;
    return null;
  }}
}}

global.Element = Element;
global.HTMLElement = HTMLElement;

const elements = Object.create(null);
const listeners = Object.create(null);
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
  addEventListener: (type, callback) => {{ listeners[type] = callback; }},
}};
global.window = {{ addEventListener: () => {{}} }};
global.crypto = {{ randomUUID: () => "qa-request-id" }};
let stateUnavailable = false;
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => stateUnavailable
      ? ({{ ok: false, message: "backend unavailable" }})
      : ({{
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

function report() {{
  console.log(JSON.stringify({{
    empty_hidden: element("product-project-empty").hidden,
    summary_hidden: element("product-project-summary").hidden,
    project_id: element("product-project-id").textContent,
    state: element("product-project-state").textContent,
    ready: document.documentElement.dataset.nikaReady || null,
  }}));
}}

eval(fs.readFileSync(process.argv[1], "utf8"));

setTimeout(() => {{
  if (!FAIL_REFRESH_AFTER_READY) {{
    report();
    return;
  }}
  stateUnavailable = true;
  const trigger = new HTMLElement("qa-refresh-trigger");
  trigger.dataset.actionId = "nav.tasks";
  listeners.click({{ target: trigger }});
  setTimeout(report, 50);
}}, 50);
"""
    result = subprocess.run(
        (_NODE, "-e", harness, str(_APP_JS)),
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


def _valid_project() -> dict[str, object]:
    return {
        "project_id": "product-" + "a" * 64,
        "spec_version": 3,
        "title": "Accessible expense app",
        "goal": "Build an accessible expense app",
        "state": "active",
        "blocker_count": 0,
        "status_count": 2,
        "decision_count": 1,
    }


def test_valid_bounded_product_project_is_exposed_semantically() -> None:
    rendered = _render_snapshot(_valid_project())

    assert rendered["ready"] == "true"
    assert rendered["empty_hidden"] is True
    assert rendered["summary_hidden"] is False
    assert rendered["project_id"] == "product-" + "a" * 64
    assert rendered["state"] == "active"


def test_malformed_mapping_cannot_claim_a_current_product_project() -> None:
    rendered = _render_snapshot({})

    assert rendered["ready"] == "true"
    assert rendered["empty_hidden"] is False
    assert rendered["summary_hidden"] is True


def test_failed_refresh_cannot_leave_stale_product_project_visible() -> None:
    rendered = _render_snapshot(
        _valid_project(),
        fail_refresh_after_ready=True,
    )

    assert rendered["ready"] == "true"
    assert rendered["empty_hidden"] is False
    assert rendered["summary_hidden"] is True
    assert rendered["project_id"] == ""
    assert rendered["state"] == ""
