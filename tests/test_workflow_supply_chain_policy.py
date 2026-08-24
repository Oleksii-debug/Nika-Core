from __future__ import annotations

import pathlib
import re


_WORKFLOW_ROOT = pathlib.Path(".github/workflows")
_ACTION_USE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_SCRIPT_PIPE = re.compile(
    r"(?:curl|wget)\b[^\n|]*https?://[^\n|]+\|\s*(?:sh|bash)\b",
    re.IGNORECASE,
)
_CHECKOUT_USE = "uses: actions/checkout@"


def _workflows() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(_WORKFLOW_ROOT.glob("*.yml"))) + tuple(
        sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
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


def test_checkout_never_persists_ci_credentials() -> None:
    findings: list[str] = []
    for workflow in _workflows():
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if _CHECKOUT_USE not in line:
                continue
            indentation = len(line) - len(line.lstrip())
            block: list[str] = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                next_indent = len(following) - len(following.lstrip())
                if stripped and next_indent <= indentation:
                    break
                block.append(following)
            normalized = "\n".join(block).casefold().replace(" ", "")
            if "persist-credentials:false" not in normalized:
                findings.append(f"{workflow}:{index + 1}")

    assert not findings, f"checkout persists CI credentials: {findings}"


def test_workflows_do_not_pipe_remote_installers_to_shells() -> None:
    findings = [
        str(workflow)
        for workflow in _workflows()
        if _REMOTE_SCRIPT_PIPE.search(workflow.read_text(encoding="utf-8"))
    ]
    assert not findings, f"remote installer piped directly to a shell: {findings}"


def test_live_ollama_proof_uses_exact_runtime_and_model_bytes() -> None:
    workflow = (_WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert 'OLLAMA_VERSION: "0.32.14"' in workflow
    assert (
        "OLLAMA_LINUX_AMD64_SHA256: "
        "c620917a71e146ab3a7f893084f066069c4c65d144ef8379a91c3cbe8b27de8f"
        in workflow
    )
    assert (
        "resolve/be21a1bc2b344d5b57381053d1dc0faea5f4e40c/"
        "SmolLM2-135M-Instruct-Q5_K_M.gguf"
        in workflow
    )
    assert (
        "NIKA_OLLAMA_PROOF_MODEL_SHA256: "
        "731d0c9cf598dada9712242ceddcca88aa0502fc8f9b8f773917df9f9113463a"
        in workflow
    )
    assert "sha256sum --check -" in workflow
    assert "ollama create" in workflow
    assert re.search(r"\bollama\s+pull\b", workflow, re.IGNORECASE) is None
