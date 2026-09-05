from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_WEB_ROOT = _ROOT / "src" / "nika_core" / "ui" / "web"
_NODE = shutil.which("node")


def _app_source() -> str:
    return (_WEB_ROOT / "app.js").read_text(encoding="utf-8")


def _html_source() -> str:
    return (_WEB_ROOT / "index.html").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_existing_web_shell_contains_semantic_three_agent_state_structure() -> None:
    html = _html_source()

    assert '<section aria-labelledby="team-task-heading">' in html
    assert '<h2 id="team-task-heading" tabindex="-1">Командне завдання</h2>' in html
    assert '<ol id="team-members-list" aria-labelledby="team-members-heading"></ol>' in html
    assert '<ol id="team-events-list" aria-labelledby="team-events-heading"></ol>' in html
    assert '<dl id="team-final-summary" aria-labelledby="team-final-heading" hidden>' in html
    assert '<a href="#team-task-heading">Командне завдання</a>' in html
    assert html.count('aria-live="') == 1


def test_renderer_uses_backend_members_only_and_polling_never_moves_focus() -> None:
    source = _app_source()
    render = _between(
        source,
        "function renderTeamTask(projection) {",
        "async function refreshState(",
    )
    polling = _between(
        source,
        "function startStatePolling() {",
        "async function initializeBridge() {",
    )

    assert "for (const member of members)" in render
    assert 'document.createElement("h4")' in source
    assert 'teamMembersList.appendChild(renderTeamMember(member))' in render
    assert "innerHTML" not in source
    assert "setInterval" in polling
    assert ".focus(" not in polling
    assert "thread_id" not in render
    assert "resume_token" not in render
    assert "tool_grants" not in render
    assert "payload_json" not in render
    assert "provider_session" not in render
    assert "authorization" not in render


def test_windows_bridge_composes_team_projection_into_existing_pywebview_state() -> None:
    source = (_ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")

    assert "from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider" in source
    assert "packaged_state = V01PackagedTeamStateProvider(" in source
    assert "base_state=product_state," in source
    assert "store=store," in source
    assert "state_provider=packaged_state," in source
    assert "launch_windows_shell(bridge" in source


def _rendered_team_snapshot() -> dict[str, object]:
    if _NODE is None:
        pytest.skip("Node.js is required for the packaged team renderer canary regression")

    canary = "PACKAGED_TEAM_RAW_SECRET_CANARY"
    projection = {
        "available": True,
        "task": {
            "task_id": "task-71",
            "state": "running",
            "command": "Перевірити два контрольовані джерела.",
        },
        "team": {
            "team_id": "team-71",
            "state": "completed",
            "member_count": 3,
            "expected_member_count": 3,
            "roster_complete": True,
        },
        "members": [
            {
                "member_id": "supervisor",
                "role": "supervisor",
                "state": "completed",
                "current_operation": "Роботу завершено.",
            },
            {
                "member_id": "worker",
                "role": "worker",
                "state": "completed",
                "current_operation": "Роботу завершено.",
            },
            {
                "member_id": "checker",
                "role": "checker",
                "state": "failed",
                "current_operation": "Роботу завершено з помилкою.",
                "safe_error": {"code": "member_failed", "message": canary},
            },
        ],
        "events": [
            {
                "code": "worker.assigned",
                "message": canary,
                "time": "2026-08-28T20:00:00+00:00",
            },
            {
                "code": "checker.error",
                "message": canary,
                "time": "2026-08-28T20:01:00+00:00",
            },
        ],
        "final_result": {
            "status": "completed",
            "summary": canary,
            "task_id": "task-71",
            "team_id": "team-71",
            "terminal_member_count": 3,
            "result_record_count": 2,
        },
        "raw_checkpoint": canary,
    }
    harness = f"""
const fs = require("fs");
const PROJECTION = {json.dumps(projection, ensure_ascii=False)};

class Element {{}}
const created = [];
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
    created.push(this);
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
global.crypto = {{ randomUUID: () => "team-ui-request-id" }};
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
  const ids = [
    "app-status",
    "team-task-id",
    "team-task-command",
    "team-task-state",
    "team-id",
    "team-state",
    "team-roster-count",
    "team-roster-note",
    "team-members-list",
    "team-events-list",
    "team-final-status",
    "team-final-text",
    "team-final-task-id",
    "team-final-team-id",
  ];
  const rendered = ids.map((id) => collect(element(id))).join("\\n");
  console.log(JSON.stringify({{
    ready: document.documentElement.dataset.nikaReady || null,
    member_count: element("team-members-list").children.length,
    summary_hidden: element("team-task-summary").hidden,
    rendered,
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
    assert lines, "Node harness produced no packaged team snapshot"
    decoded = json.loads(lines[-1])
    assert isinstance(decoded, dict)
    return decoded


def test_renderer_shows_three_real_members_and_never_echoes_raw_secret_fields() -> None:
    rendered = _rendered_team_snapshot()
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert rendered["member_count"] == 3
    assert rendered["summary_hidden"] is False
    assert "Координатор" in text
    assert "Виконавець" in text
    assert "Перевіряльник" in text
    assert "Перевірити два контрольовані джерела." in text
    assert "Виконання учасника завершилося помилкою." in text
    assert "Перевіряльник завершив операцію з помилкою." in text
    assert "PACKAGED_TEAM_RAW_SECRET_CANARY" not in text
