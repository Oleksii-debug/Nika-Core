from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider

_ROOT = Path(__file__).parents[1]
_APP_JS = _ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
_NODE = shutil.which("node")


def test_paused_member_operation_is_truthful_and_bounded() -> None:
    assert (
        V01PackagedTeamStateProvider._operation(role="worker", state="paused")
        == "Роботу призупинено."
    )


def test_webview_renderer_accepts_paused_member_without_losing_focus() -> None:
    if _NODE is None:
        pytest.skip("Node.js is required for the packaged paused-state renderer regression")

    projection = {
        "available": True,
        "task": {
            "task_id": "task-paused-71",
            "state": "paused",
            "command": "Продовжити контрольоване завдання.",
        },
        "team": {
            "team_id": "team-paused-71",
            "state": "active",
            "member_count": 2,
            "expected_member_count": 3,
            "roster_complete": False,
        },
        "members": [
            {
                "member_id": "supervisor",
                "role": "supervisor",
                "state": "running",
                "current_operation": "Координує командне завдання.",
            },
            {
                "member_id": "worker",
                "role": "worker",
                "state": "paused",
                "current_operation": "Роботу призупинено.",
            },
        ],
        "events": [],
        "final_result": None,
    }
    harness = f"""
const fs = require("fs");
const PROJECTION = {json.dumps(projection, ensure_ascii=False)};

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
    this.value = "";
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
function collect(node) {{
  if (!node) return "";
  return [node.textContent, ...node.children.map(collect)].join("\\n");
}}
const focusSentinel = new HTMLElement("focus-sentinel");

global.document = {{
  activeElement: focusSentinel,
  hidden: false,
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
global.window = {{
  addEventListener: () => {{}},
  setInterval: () => 1,
  clearInterval: () => {{}},
}};
global.crypto = {{ randomUUID: () => "paused-ui-request-id" }};
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => ({{
      ok: true,
      state: {{
        tasks: [],
        agents: [],
        workspaces: [],
        product_project: null,
        v01_team_task: PROJECTION,
      }},
    }}),
  }},
}};

eval(fs.readFileSync(process.argv[1], "utf8"));

setTimeout(() => {{
  const rendered = [
    collect(element("team-task-state")),
    collect(element("team-members-list")),
  ].join("\\n");
  console.log(JSON.stringify({{
    ready: document.documentElement.dataset.nikaReady || null,
    summary_hidden: element("team-task-summary").hidden,
    member_count: element("team-members-list").children.length,
    focus_preserved: document.activeElement === focusSentinel,
    rendered,
  }}));
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
    assert lines, "Node harness produced no paused-state snapshot"
    rendered = json.loads(lines[-1])

    assert rendered["ready"] == "true"
    assert rendered["summary_hidden"] is False
    assert rendered["member_count"] == 2
    assert rendered["focus_preserved"] is True
    assert "paused" in rendered["rendered"]
    assert "Роботу призупинено." in rendered["rendered"]
    assert "Стан командного завдання недоступний." not in rendered["rendered"]
