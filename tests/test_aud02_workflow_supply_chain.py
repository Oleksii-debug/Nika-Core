from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW_ROOT = Path(".github/workflows")
_ACTION_USE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_SCRIPT_PIPE = re.compile(
    r"(?:curl|wget)\b[^\n|]*https?://[^\n|]+\|\s*(?:sh|bash)\b",
    re.IGNORECASE,
)
_MUTABLE_OLLAMA_PULL = re.compile(r"\bollama\s+pull\s+([^\s#]+)", re.IGNORECASE)
_STEP_ITEM = re.compile(r"^(?P<indent>\s*)-\s+")
_PERSIST_FALSE = re.compile(
    r"^\s*persist-credentials:\s*false\s*(?:#.*)?$",
    re.IGNORECASE,
)


def _workflows() -> tuple[Path, ...]:
    return tuple(sorted(_WORKFLOW_ROOT.glob("*.yml"))) + tuple(
        sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )


def _step_bounds(lines: list[str], uses_index: int) -> tuple[int, int]:
    uses_line = lines[uses_index]
    uses_indent = len(uses_line) - len(uses_line.lstrip())
    step_start: int | None = None
    step_indent: int | None = None

    for candidate in range(uses_index, -1, -1):
        match = _STEP_ITEM.match(lines[candidate])
        if not match:
            continue
        candidate_indent = len(match.group("indent"))
        if candidate == uses_index or candidate_indent < uses_indent:
            step_start = candidate
            step_indent = candidate_indent
            break

    if step_start is None or step_indent is None:
        raise AssertionError(f"checkout at line {uses_index + 1} is not inside a YAML list step")

    step_end = len(lines)
    for candidate in range(step_start + 1, len(lines)):
        match = _STEP_ITEM.match(lines[candidate])
        if match and len(match.group("indent")) == step_indent:
            step_end = candidate
            break

    return step_start, step_end


def _checkout_disables_persisted_credentials(lines: list[str], uses_index: int) -> bool:
    step_start, step_end = _step_bounds(lines, uses_index)
    return any(_PERSIST_FALSE.fullmatch(line) for line in lines[step_start:step_end])


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
    """Every workflow is security-relevant; mutable action tags are not acceptable evidence."""
    mutable: list[str] = []
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        for action_ref in _ACTION_USE.findall(text):
            if action_ref.startswith("./"):
                continue
            action, separator, ref = action_ref.rpartition("@")
            if not separator or not action or not _FULL_COMMIT_SHA.fullmatch(ref):
                mutable.append(f"{workflow}:{action_ref}")

    assert not mutable, f"mutable third-party action references: {mutable}"


def test_workflows_do_not_pipe_mutable_remote_installers_to_a_shell() -> None:
    findings: list[str] = []
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        if _REMOTE_SCRIPT_PIPE.search(text):
            findings.append(str(workflow))

    assert not findings, f"remote installer piped directly to a shell: {findings}"


def test_model_downloads_do_not_use_mutable_ollama_pull_identity() -> None:
    mutable: list[str] = []
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        for model_ref in _MUTABLE_OLLAMA_PULL.findall(text):
            if "@sha256:" not in model_ref.casefold():
                mutable.append(f"{workflow}:{model_ref}")

    assert not mutable, f"model downloads use mutable Ollama tag/name identity: {mutable}"


def test_checkout_step_parser_handles_named_steps_and_negative_controls() -> None:
    action = "actions/checkout@" + ("a" * 40)
    positive = [
        "      - name: Checkout exact candidate",
        f"        uses: {action}",
        "        with:",
        "          ref: ${{ github.event.pull_request.head.sha }}",
        "          persist-credentials: false",
    ]
    missing = [
        "      - name: Checkout without credential policy",
        f"        uses: {action}",
        "        with:",
        "          ref: ${{ github.event.pull_request.head.sha }}",
    ]
    explicit_true = [
        "      - name: Unsafe checkout",
        f"        uses: {action}",
        "        with:",
        "          persist-credentials: true",
    ]

    assert _checkout_disables_persisted_credentials(positive, 1)
    assert not _checkout_disables_persisted_credentials(missing, 1)
    assert not _checkout_disables_persisted_credentials(explicit_true, 1)


def test_checkout_never_persists_ci_credentials() -> None:
    """Read-only verification jobs must not leave the CI token in Git configuration."""
    findings: list[str] = []
    for workflow in _workflows():
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/checkout@" not in line:
                continue
            if not _checkout_disables_persisted_credentials(lines, index):
                findings.append(f"{workflow}:{index + 1}")

    assert not findings, f"checkout persists CI credentials: {findings}"
