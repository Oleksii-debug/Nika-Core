from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from time import monotonic, sleep

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
    assert '<dd id="team-final-model-text">—</dd>' in html
    assert '<dd id="team-final-model-provider">—</dd>' in html
    assert '<dd id="team-final-model-name">—</dd>' in html
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
    assert "teamMembersList.appendChild(renderTeamMember(member))" in render
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
    assert "state_provider=source_state," in source
    assert '**packaged_state(), "v01_sources": source_settings.snapshot()' in source
    assert "launch_windows_shell(bridge" in source


def _rendered_team_snapshot(
    *,
    live_projection: dict[str, object] | None = None,
    next_projection: dict[str, object] | None = None,
    next_recovery: dict[str, object] | None = None,
    stale_projection: dict[str, object] | None = None,
) -> dict[str, object]:
    if _NODE is None:
        pytest.skip("Node.js is required for the packaged team renderer canary regression")

    canary = "PACKAGED_TEAM_RAW_SECRET_CANARY"
    projection = {
        "available": True,
        "task": {
            "task_id": "task-71",
            "state": "RUNNING",
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
    if live_projection is not None:
        projection = live_projection
    harness = f"""
const fs = require("fs");
const PROJECTION = {json.dumps(projection, ensure_ascii=False)};
const NEXT_PROJECTION = {json.dumps(next_projection, ensure_ascii=False)};
const NEXT_RECOVERY = {json.dumps(next_recovery, ensure_ascii=False)};
const STALE_PROJECTION = {json.dumps(stale_projection, ensure_ascii=False)};
let getStateCalls = 0;

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
  setInterval: (callback) => {{
    if (STALE_PROJECTION !== null) {{
      setTimeout(() => {{ void callback(); }}, 5);
      setTimeout(() => {{ void callback(); }}, 10);
    }} else if (NEXT_PROJECTION !== null) {{
      setTimeout(() => {{ void callback(); }}, 5);
    }}
    return 1;
  }},
  clearInterval: () => {{}},
}};
function initialRecovery() {{
  return {{
    schema_version: 1,
    status: "ready",
    auto_resume_count: 0,
    manual_resume_count: 0,
    approval_count: 0,
    uncertain_count: 0,
    blocked_count: 0,
    resume_failed_count: 0,
  }};
}}
async function getState() {{
  const call = getStateCalls;
  getStateCalls += 1;
  let projection = PROJECTION;
  let recovery = initialRecovery();
  let delay = 0;
  if (call > 0) {{
    if (STALE_PROJECTION !== null && call === 1) {{
      projection = STALE_PROJECTION;
      delay = 40;
    }} else if (NEXT_PROJECTION !== null) {{
      projection = NEXT_PROJECTION;
    }}
    if (NEXT_RECOVERY !== null) recovery = NEXT_RECOVERY;
  }}
  if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
  return {{
    ok: true,
    state: {{
      tasks: [],
      agents: [],
      workspaces: [],
      startup_recovery: recovery,
      product_project: null,
      v01_team_task: projection,
    }},
  }};
}}
global.crypto = {{ randomUUID: () => "team-ui-request-id" }};
global.pywebview = {{
  api: {{
    list_actions: async () => [],
    get_state: getState,
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
    "team-final-model-text",
    "team-final-model-provider",
    "team-final-model-name",
  ];
  const rendered = ids.map((id) => collect(element(id))).join("\\n");
  console.log(JSON.stringify({{
    ready: document.documentElement.dataset.nikaReady || null,
    member_count: element("team-members-list").children.length,
    summary_hidden: element("team-task-summary").hidden,
    rendered,
  }}));
}}, STALE_PROJECTION !== null ? 90 : 50);
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


def test_renderer_accepts_actual_packaged_team_and_rejects_duplicate_member_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nika_core.config import AppConfig
    from nika_core.data.sqlite import SQLiteStore
    from nika_core.kernel.task_queue import TaskQueue
    from nika_core.kernel.task_state import TaskState
    from nika_core.ui.desktop_backend import DesktopBackend
    from scripts.nika_windows import build_windows_bridge

    source_root = tmp_path / "Джерела команди"
    source_root.mkdir()
    for name in ("А.txt", "Б.txt"):
        (source_root / name).write_text("Контрольоване спільне свідчення.", encoding="utf-8")
    config = AppConfig(database_path=tmp_path / "nika.db")
    pending = []
    original_start = DesktopBackend._schedule_start
    monkeypatch.setattr(
        DesktopBackend,
        "_schedule_start",
        lambda self, task, command: pending.append((self, task, command)),
    )
    bridge, _ = build_windows_bridge(config)
    assert (
        bridge.dispatch(
            {
                "request_id": "live-sources",
                "action_id": "team.sources.configure",
                "payload": {
                    "root": str(source_root),
                    "source_a": "А.txt",
                    "source_b": "Б.txt",
                    "revision": 0,
                },
            }
        )["status"]
        == "completed"
    )
    assert (
        bridge.dispatch(
            {
                "request_id": "live-model",
                "action_id": "settings.model.configure",
                "payload": {
                    "revision": 0,
                    "route_kind": "deterministic",
                    "provider_id": None,
                    "model": None,
                    "base_url": None,
                    "credential_ref": None,
                    "private_data_allowed": True,
                    "timeout_seconds": 60,
                },
            }
        )["status"]
        == "completed"
    )
    assert (
        bridge.dispatch(
            {
                "request_id": "live-task",
                "action_id": "task.create",
                "payload": {"command": "Порівняй джерела"},
            }
        )["status"]
        == "accepted"
    )
    backend, task_id, command = pending.pop()
    original_start(backend, task_id, command)
    queue = TaskQueue(SQLiteStore(config.database_path))
    deadline = monotonic() + 20
    while queue.get(task_id).state in {TaskState.READY, TaskState.RUNNING}:
        assert monotonic() < deadline, "Packaged team did not finish within 20 seconds"
        sleep(0.01)
    backend.close()
    assert queue.get(task_id).state is TaskState.COMPLETED
    projection = bridge.get_state()["state"]["v01_team_task"]
    assert projection["team"]["state"] == "completed"
    assert sorted(member["role"] for member in projection["members"]) == [
        "checker",
        "worker",
        "worker",
    ]
    rendered = _rendered_team_snapshot(live_projection=projection)
    assert rendered["ready"] == "true"
    assert rendered["member_count"] == 3
    assert rendered["summary_hidden"] is False
    completed_text = "Командне завдання завершено; збережені результати учасників доступні."
    assert completed_text in rendered["rendered"]
    assert completed_text in (_ROOT / "scripts" / "m5_uia_proof.ps1").read_text(encoding="utf-8")
    partial = json.loads(json.dumps(projection))
    partial["members"] = [
        member for member in partial["members"] if member["member_id"] != "worker-b"
    ]
    partial["team"].update(member_count=2, roster_complete=False, state="active")
    partial["final_result"] = None
    waiting = _rendered_team_snapshot(live_projection=partial)
    assert waiting["ready"] == "true"
    assert waiting["member_count"] == 2
    assert completed_text not in waiting["rendered"]
    partial["final_result"] = projection["final_result"]
    assert _rendered_team_snapshot(live_projection=partial)["summary_hidden"] is True
    for fault in ("duplicate_identity", "missing_checker"):
        invalid = json.loads(json.dumps(projection))
        if fault == "duplicate_identity":
            invalid["members"][1]["member_id"] = invalid["members"][0]["member_id"]
        else:
            for member in invalid["members"]:
                member["role"] = "worker"
        rejected = _rendered_team_snapshot(live_projection=invalid)
        assert rejected["ready"] == "false"
        assert rejected["summary_hidden"] is True
        assert rejected["member_count"] == 0


def _model_result_projection() -> dict[str, object]:
    return {
        "available": True,
        "task": {
            "task_id": "task-model-result",
            "state": "COMPLETED",
            "command": "Покажи перевірену відповідь моделі.",
        },
        "team": {
            "team_id": "team-model-result",
            "state": "completed",
            "member_count": 3,
            "expected_member_count": 3,
            "roster_complete": True,
        },
        "members": [
            {
                "member_id": "checker",
                "role": "checker",
                "state": "completed",
                "current_operation": "Роботу завершено.",
            },
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
        ],
        "events": [],
        "final_result": {
            "status": "completed",
            "summary": "bounded",
            "task_id": "task-model-result",
            "team_id": "team-model-result",
            "terminal_member_count": 3,
            "result_record_count": 3,
            "comparison": {
                "status": "agree",
                "validated": True,
                "source_states": ["valid", "valid"],
                "agreement_count": 1,
                "difference_count": 0,
                "model_result": {
                    "text": "Перевірена відповідь <b>лишається текстом</b> & не HTML.",
                    "provider_id": "ollama",
                    "provider_kind": "local",
                    "model": "qwen2.5:7b",
                    "provenance_validated": True,
                },
            },
        },
    }


def test_renderer_exposes_only_validated_bounded_model_result_as_semantic_text() -> None:
    projection = _model_result_projection()
    rendered = _rendered_team_snapshot(live_projection=projection)
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert rendered["summary_hidden"] is False
    assert "Перевірена відповідь <b>лишається текстом</b> & не HTML." in text
    assert "ollama" in text
    assert "qwen2.5:7b" in text
    assert "innerHTML" not in _app_source()


def test_renderer_rejects_model_result_with_unknown_authority_fields() -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    model_result = comparison["model_result"]
    assert isinstance(model_result, dict)
    model_result["raw_provenance"] = "MODEL_RESULT_SECRET_CANARY"

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True
    assert "MODEL_RESULT_SECRET_CANARY" not in str(rejected["rendered"])


@pytest.mark.parametrize(
    "invalid_text",
    [
        " leading boundary whitespace",
        "trailing boundary whitespace ",
        "contains\x00nul",
        "x" * 2001,
    ],
)
def test_renderer_rejects_noncanonical_model_text(invalid_text: str) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    model_result = comparison["model_result"]
    assert isinstance(model_result, dict)
    model_result["text"] = invalid_text

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("provider_id", " leading-provider"),
        ("provider_id", "x" * 129),
        ("provider_id", "provider\x1fcontrol"),
        ("provider_id", "provider\x7fdelete"),
        ("provider_kind", "no_llm"),
        ("provider_kind", "LOCAL"),
        ("model", "trailing-model "),
        ("model", "x" * 513),
        ("model", "model\x1fcontrol"),
    ],
)
def test_renderer_rejects_noncanonical_model_identity(
    field: str,
    invalid_value: str,
) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    model_result = comparison["model_result"]
    assert isinstance(model_result, dict)
    model_result[field] = invalid_value

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize(
    ("team_state", "final_status"),
    [
        ("failed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_renderer_rejects_model_result_outside_coherent_completed_state(
    team_state: str,
    final_status: str,
) -> None:
    projection = _model_result_projection()
    team = projection["team"]
    final_result = projection["final_result"]
    assert isinstance(team, dict)
    assert isinstance(final_result, dict)
    team["state"] = team_state
    final_result["status"] = final_status

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True
    assert "Перевірена відповідь <b>лишається текстом</b> & не HTML." not in str(
        rejected["rendered"]
    )


@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
def test_renderer_rejects_terminal_team_without_durable_final_result(
    terminal_state: str,
) -> None:
    projection = _model_result_projection()
    team = projection["team"]
    assert isinstance(team, dict)
    team["state"] = terminal_state
    projection["final_result"] = None

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


def test_renderer_rejects_active_team_with_terminal_final_result() -> None:
    projection = _model_result_projection()
    team = projection["team"]
    assert isinstance(team, dict)
    team["state"] = "active"

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


def test_renderer_presents_unknown_packaged_task_state_without_raw_leak() -> None:
    projection = _model_result_projection()
    task = projection["task"]
    assert isinstance(task, dict)
    task["state"] = "FUTURE_TASK_STATE"

    rendered = _rendered_team_snapshot(live_projection=projection)
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert rendered["summary_hidden"] is False
    assert "Стан недоступний" in text
    assert "FUTURE_TASK_STATE" not in text


def test_renderer_rejects_blank_packaged_task_state() -> None:
    projection = _model_result_projection()
    task = projection["task"]
    assert isinstance(task, dict)
    task["state"] = "   "

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize(
    ("status", "agreement_count", "difference_count"),
    [
        ("agree", 1, 1),
        ("disagree", 1, 1),
        ("partial", 0, 1),
        ("partial", 1, 0),
    ],
)
def test_renderer_rejects_incoherent_valid_comparison_counts(
    status: str,
    agreement_count: int,
    difference_count: int,
) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.update(
        status=status,
        validated=True,
        source_states=["valid", "valid"],
        agreement_count=agreement_count,
        difference_count=difference_count,
    )

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize("agreement_count", [101, (1 << 53)])
def test_renderer_rejects_out_of_bound_partial_agreement_count(
    agreement_count: int,
) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.update(
        status="partial",
        validated=True,
        source_states=["valid", "valid"],
        agreement_count=agreement_count,
        difference_count=1,
    )

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


def test_renderer_rejects_noncomparison_with_comparison_counts() -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")
    comparison.update(
        status="worker_error",
        validated=False,
        source_states=["worker_error", "valid"],
        agreement_count=1,
        difference_count=0,
    )

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize(
    ("status", "source_states", "agreement_count", "difference_count"),
    [
        ("agree", ["valid", "missing"], 1, 0),
        ("missing", ["valid", "valid"], 0, 0),
        ("worker_error", ["evidence_invalid", "worker_error"], 0, 0),
        ("missing", ["worker_error", "missing"], 0, 0),
        ("evidence_invalid", ["missing", "valid"], 0, 0),
    ],
)
def test_renderer_rejects_status_that_conflicts_with_source_states(
    status: str,
    source_states: list[str],
    agreement_count: int,
    difference_count: int,
) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")
    comparison.update(
        status=status,
        validated=False,
        source_states=source_states,
        agreement_count=agreement_count,
        difference_count=difference_count,
    )

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


@pytest.mark.parametrize(
    ("status", "source_states"),
    [
        ("evidence_invalid", ["evidence_invalid", "worker_error"]),
        ("worker_error", ["worker_error", "missing"]),
        ("missing", ["missing", "valid"]),
    ],
)
def test_renderer_accepts_canonical_noncomparison_source_state_precedence(
    status: str,
    source_states: list[str],
) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")
    comparison.update(
        status=status,
        validated=False,
        source_states=source_states,
        agreement_count=0,
        difference_count=0,
    )

    rendered = _rendered_team_snapshot(live_projection=projection)

    assert rendered["ready"] == "true"
    assert rendered["summary_hidden"] is False


def test_renderer_accepts_canonical_evidence_invalid_without_source_details() -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")
    comparison.update(
        status="evidence_invalid",
        validated=False,
        source_states=[],
        agreement_count=0,
        difference_count=0,
    )

    rendered = _rendered_team_snapshot(live_projection=projection)

    assert rendered["ready"] == "true"
    assert rendered["summary_hidden"] is False


@pytest.mark.parametrize("status", ["agree", "missing", "worker_error"])
def test_renderer_rejects_empty_source_details_for_other_statuses(status: str) -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")
    comparison.update(
        status=status,
        validated=False,
        source_states=[],
        agreement_count=0,
        difference_count=0,
    )

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


def test_renderer_rejects_terminal_member_count_not_matching_roster_state() -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    final_result["terminal_member_count"] = 2

    rejected = _rendered_team_snapshot(live_projection=projection)

    assert rejected["ready"] == "false"
    assert rejected["summary_hidden"] is True


def test_renderer_uses_explicit_no_model_fallback_for_deterministic_result() -> None:
    projection = _model_result_projection()
    final_result = projection["final_result"]
    assert isinstance(final_result, dict)
    comparison = final_result["comparison"]
    assert isinstance(comparison, dict)
    comparison.pop("model_result")

    rendered = _rendered_team_snapshot(live_projection=projection)
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert "Немає перевіреної відповіді моделі для цього результату." in text
    assert text.count("Не застосовується") >= 2


def test_renderer_is_restart_stable_for_identical_durable_model_projection() -> None:
    projection = _model_result_projection()

    first = _rendered_team_snapshot(live_projection=projection)
    reopened = _rendered_team_snapshot(
        live_projection=json.loads(json.dumps(projection, ensure_ascii=False))
    )

    assert first["ready"] == reopened["ready"] == "true"
    assert first["rendered"] == reopened["rendered"]


def test_polling_announces_task_state_only_transition() -> None:
    before = _model_result_projection()
    before_task = before["task"]
    assert isinstance(before_task, dict)
    before_task["state"] = "RUNNING"

    after = json.loads(json.dumps(before, ensure_ascii=False))
    after_task = after["task"]
    assert isinstance(after_task, dict)
    after_task["state"] = "PAUSED"

    rendered = _rendered_team_snapshot(
        live_projection=before,
        next_projection=after,
    )
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert "Призупинено" in text
    assert "Стан командного завдання оновлено." in text
    assert "Перевірена відповідь моделі доступна" not in text


def test_polling_preserves_model_result_announcement_during_recovery_transition() -> None:
    before = _model_result_projection()
    final_before = before["final_result"]
    assert isinstance(final_before, dict)
    comparison_before = final_before["comparison"]
    assert isinstance(comparison_before, dict)
    comparison_before.pop("model_result")
    attention_recovery = {
        "schema_version": 1,
        "status": "attention",
        "auto_resume_count": 0,
        "manual_resume_count": 0,
        "approval_count": 0,
        "uncertain_count": 1,
        "blocked_count": 0,
        "resume_failed_count": 0,
    }

    rendered = _rendered_team_snapshot(
        live_projection=before,
        next_projection=_model_result_projection(),
        next_recovery=attention_recovery,
    )

    assert rendered["ready"] == "true"
    assert "Невизначена або заблокована робота." in rendered["rendered"]
    assert (
        "Перевірена відповідь моделі доступна в підсумку командного завдання."
        in rendered["rendered"]
    )


def test_polling_discards_older_state_response_after_newer_model_result() -> None:
    before = _model_result_projection()
    final_before = before["final_result"]
    assert isinstance(final_before, dict)
    comparison_before = final_before["comparison"]
    assert isinstance(comparison_before, dict)
    comparison_before.pop("model_result")

    stale = json.loads(json.dumps(before, ensure_ascii=False))
    stale_task = stale["task"]
    assert isinstance(stale_task, dict)
    stale_task["command"] = "STALE_STATE_MUST_NOT_WIN"

    latest = _model_result_projection()
    latest_task = latest["task"]
    assert isinstance(latest_task, dict)
    latest_task["command"] = "LATEST_STATE_MUST_WIN"

    rendered = _rendered_team_snapshot(
        live_projection=before,
        next_projection=latest,
        stale_projection=stale,
    )
    text = str(rendered["rendered"])

    assert rendered["ready"] == "true"
    assert "LATEST_STATE_MUST_WIN" in text
    assert "STALE_STATE_MUST_NOT_WIN" not in text
    assert "Перевірена відповідь <b>лишається текстом</b> & не HTML." in text
    assert (
        "Перевірена відповідь моделі доступна в підсумку командного завдання."
        in text
    )


def test_polling_announces_when_validated_model_result_becomes_available() -> None:
    before = _model_result_projection()
    final_before = before["final_result"]
    assert isinstance(final_before, dict)
    comparison_before = final_before["comparison"]
    assert isinstance(comparison_before, dict)
    comparison_before.pop("model_result")

    rendered = _rendered_team_snapshot(
        live_projection=before,
        next_projection=_model_result_projection(),
    )

    assert rendered["ready"] == "true"
    assert (
        "Перевірена відповідь моделі доступна в підсумку командного завдання."
        in rendered["rendered"]
    )
