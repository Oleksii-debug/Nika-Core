from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_WEB_ROOT = Path(__file__).parents[1] / "src" / "nika_core" / "ui" / "web"
_NODE = shutil.which("node")


def _source() -> str:
    return (_WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_state_failure_hides_stale_product_project_projection() -> None:
    source = _source()
    unavailable = _between(
        source,
        "function renderProductProjectUnavailable(message) {",
        "function reportStateUnavailable() {",
    )
    report = _between(
        source,
        "function reportStateUnavailable() {",
        "function renderProductProject(project) {",
    )
    refresh = _between(
        source,
        "async function refreshState(",
        "async function dispatch(actionId, trigger = null) {",
    )

    assert 'productProjectEmpty.hidden = false;' in unavailable
    assert 'productProjectSummary.hidden = true;' in unavailable
    assert 'clearProductProjectFields();' in unavailable
    assert "renderProductProjectUnavailable(productProjectUnavailableMessage);" in report
    assert "announce(productProjectUnavailableMessage, true);" in report
    assert "appendLog(productProjectUnavailableMessage);" in report
    assert refresh.count("reportStateUnavailable();") == 3
    assert "response = await globalThis.pywebview.api.get_state();" in refresh
    assert "catch {" in refresh
    assert "if (!response?.ok)" in refresh


def test_state_failure_does_not_echo_raw_backend_diagnostics() -> None:
    source = _source()
    refresh = _between(
        source,
        "async function refreshState(",
        "async function dispatch(actionId, trigger = null) {",
    )

    assert "error.message" not in refresh
    assert "String(error)" not in refresh
    assert "appendLog(response.message" not in refresh
    assert "announce(response.message" not in refresh


def test_initialization_preserves_truthful_state_failure_status() -> None:
    source = _source()
    initialize = _between(
        source,
        "async function initializeBridge() {",
        'document.getElementById("keymap-export")',
    )

    state_refresh = "stateReady = await refreshState({ announceTeamTransitions: false });"
    state_guard = "if (!stateReady) {"
    ready_marker = 'document.documentElement.dataset.nikaReady = "true";'
    assert state_refresh in initialize
    assert state_guard in initialize
    assert initialize.index(state_refresh) < initialize.index(state_guard)
    assert initialize.index(state_guard) < initialize.index(ready_marker)
    assert 'throw new Error("Desktop state bridge unavailable")' not in initialize
    assert 'announce("Не вдалося завантажити команди Nika Core.", true);' in initialize
    assert "error.message" not in initialize
    assert "String(error)" not in initialize


def _state_failure_snapshot(mode: str) -> dict[str, object]:
    if _NODE is None:
        pytest.skip("Node.js is required for the state failure canary regression")

    canary = "STATE_FAILURE_CANARY_SECRET_PATH_TOKEN"
    harness = f"""
const fs = require("fs");
const MODE = {json.dumps(mode)};
const CANARY = {json.dumps(canary)};

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
global.crypto = {{ randomUUID: () => "state-canary-request-id" }};

element("product-project-id").textContent = "stale-project-id";
element("product-project-state").textContent = "active";
element("product-project-summary").hidden = false;

global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => {{
      if (MODE === "throw") throw new Error(CANARY);
      return {{ ok: false, message: CANARY }};
    }},
  }},
}};

eval(fs.readFileSync(process.argv[1], "utf8"));

setTimeout(() => {{
  const logText = element("activity-log").children.map((node) => node.textContent).join("\n");
  const renderedText = [
    element("app-status").textContent,
    element("product-project-empty").textContent,
    element("product-project-id").textContent,
    element("product-project-state").textContent,
    logText,
  ].join("\n");
  console.log(JSON.stringify({{
    ready: document.documentElement.dataset.nikaReady || null,
    summary_hidden: element("product-project-summary").hidden,
    project_id: element("product-project-id").textContent,
    state: element("product-project-state").textContent,
    rendered_text: renderedText,
  }}));
}}, 50);
"""
    result = subprocess.run(
        (_NODE, "-e", harness, str(_WEB_ROOT / "app.js")),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "Node harness produced no state failure snapshot"
    decoded = json.loads(lines[-1])
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.parametrize("mode", ["throw", "rejected"])
def test_state_failure_canary_is_minimized_and_readiness_stays_false(mode: str) -> None:
    rendered = _state_failure_snapshot(mode)

    assert rendered["ready"] == "false"
    assert rendered["summary_hidden"] is True
    assert rendered["project_id"] == ""
    assert rendered["state"] == ""
    assert "STATE_FAILURE_CANARY_SECRET_PATH_TOKEN" not in rendered["rendered_text"]
    assert "Стан поточного ProductProject недоступний." in rendered["rendered_text"]


def test_repair_preserves_existing_keymap_binding_contract() -> None:
    source = _source()

    assert 'action.may_be_unbound ? "Зберегти / очистити" : "Зберегти"' in source
    assert "may_beUnbound" not in source
