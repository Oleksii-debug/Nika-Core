from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_APP = _ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
_NODE = shutil.which("node")


def _app_source() -> str:
    return _APP.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def _rendered_status_text(*, task_state: str, list_state: str) -> str:
    if _NODE is None:
        pytest.skip("Node.js is required for packaged status-presentation regression")

    projection = {
        "available": True,
        "task": {
            "task_id": "task-status-1",
            "state": task_state,
            "command": "Перевірити доступний стан.",
        },
        "team": {
            "team_id": "team-status-1",
            "state": "completed",
            "member_count": 3,
            "expected_member_count": 3,
            "roster_complete": True,
        },
        "members": [
            {
                "member_id": "worker-a",
                "role": "worker",
                "state": "completed",
                "current_operation": "Роботу завершено.",
            },
            {
                "member_id": "worker-b",
                "role": "worker",
                "state": "completed",
                "current_operation": "Роботу завершено.",
            },
            {
                "member_id": "checker",
                "role": "checker",
                "state": "failed",
                "current_operation": "Роботу завершено з помилкою.",
                "safe_error": {"code": "member_failed"},
            },
        ],
        "events": [],
        "final_result": {
            "status": "completed",
            "task_id": "task-status-1",
            "team_id": "team-status-1",
            "terminal_member_count": 3,
            "result_record_count": 2,
        },
    }
    harness = f"""
const fs = require("fs");
const PROJECTION = {json.dumps(projection, ensure_ascii=False)};
const LIST_STATE = {json.dumps(list_state)};

class Element {{}}
class HTMLElement extends Element {{
  constructor(id = "") {{
    super();
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.type = "text";
    this.tagName = "DIV";
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
function collect(node) {{
  if (!node) return "";
  return [node.textContent, ...node.children.map(collect)].join("\\n");
}}

global.document = {{
  activeElement: null,
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
global.crypto = {{ randomUUID: () => "status-presentation-request" }};
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: async () => ({{
      ok: true,
      state: {{
        tasks: [{{ task_id: "task-list-1", command: "Стан списку", state: LIST_STATE }}],
        agents: [],
        workspaces: [],
        startup_recovery: {{
          schema_version: 1,
          status: "ready",
          auto_resume_count: 0,
          manual_resume_count: 0,
          approval_count: 0,
          uncertain_count: 0,
          blocked_count: 0,
          resume_failed_count: 0,
        }},
        product_project: null,
        v01_team_task: PROJECTION,
      }},
    }}),
  }},
}};

eval(fs.readFileSync(process.argv[1], "utf8"));

setTimeout(() => {{
  const ids = [
    "tasks-list",
    "team-task-state",
    "team-state",
    "team-members-list",
    "team-final-status",
  ];
  console.log(JSON.stringify({{
    ready: document.documentElement.dataset.nikaReady || null,
    rendered: ids.map((id) => collect(element(id))).join("\\n"),
  }}));
}}, 50);
"""
    result = subprocess.run(
        (_NODE, "-e", harness, str(_APP)),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "Node harness produced no status-presentation snapshot"
    decoded = json.loads(lines[-1])
    assert decoded["ready"] == "true"
    return str(decoded["rendered"])


def test_packaged_statuses_are_ukrainian_and_raw_internal_tokens_are_hidden() -> None:
    rendered = _rendered_status_text(task_state="RUNNING", list_state="RUNNING")

    assert "Виконується" in rendered
    assert "Завершено" in rendered
    assert "Завершено з помилкою" in rendered
    for raw_token in ("RUNNING", "completed", "failed"):
        assert raw_token not in rendered


def test_unknown_task_status_fails_closed_to_ukrainian_unavailable_label() -> None:
    rendered = _rendered_status_text(
        task_state="FUTURE_INTERNAL_STATE",
        list_state="UNRECOGNIZED_TASK_STATE",
    )

    assert rendered.count("Стан недоступний") >= 2
    assert "FUTURE_INTERNAL_STATE" not in rendered
    assert "UNRECOGNIZED_TASK_STATE" not in rendered


def test_status_renderer_does_not_directly_echo_internal_state_fields() -> None:
    source = _app_source()

    for raw_assignment in (
        'appendDefinitionItem(details, "Стан", member.state);',
        "teamTaskFields.task_state.textContent = task.state;",
        "teamTaskFields.team_state.textContent = team.state;",
        "teamFinalFields.status.textContent = finalResult.status;",
        '${item.command || "Без назви"} — ${item.state}',
    ):
        assert raw_assignment not in source
    assert "presentState(memberStateLabels, member.state)" in source
    assert "presentState(taskStateLabels, task.state)" in source
    assert "presentState(teamStateLabels, team.state)" in source
    assert "presentState(finalStatusLabels, finalResult.status)" in source


def test_generic_dispatch_commits_directed_focus_before_state_refresh() -> None:
    dispatch = _between(
        _app_source(),
        "async function dispatch(actionId, trigger = null) {",
        "async function refreshKeymap() {",
    )

    focus_decl = dispatch.index("const focusId =")
    focus_commit = dispatch.index("if (focusId) focusElementById(focusId);")
    refresh = dispatch.index("const stateReady = await refreshState();")
    assert focus_decl < focus_commit < refresh
