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


def _workflows() -> tuple[Path, ...]:
    return tuple(sorted(_WORKFLOW_ROOT.glob("*.yml"))) + tuple(
        sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )


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


def test_checkout_never_persists_ci_credentials() -> None:
    """Read-only verification jobs do not need the checkout token persisted in Git config."""
    findings: list[str] = []
    for workflow in _workflows():
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/checkout@" not in line:
                continue
            indentation = len(line) - len(line.lstrip())
            block: list[str] = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if stripped and len(following) - len(following.lstrip()) <= indentation:
                    break
                block.append(following)
            normalized = "\n".join(block).casefold().replace(" ", "")
            if "persist-credentials:false" not in normalized:
                findings.append(f"{workflow}:{index + 1}")

    assert not findings, f"checkout persists CI credentials: {findings}"
